"""Telegram event routing, authorization, private settings and join challenge."""
import json
import logging
import os
import random
import secrets
import time
from collections import defaultdict

from .api import TelegramAPIError
from .economy import Economy
from .raffle import Raffles
from .moderation import FloodGuard, PhotoRepeatGuard, evaluate, normalize
from .ocr import OCRWorker, MAX_OCR_FILE_BYTES, matched_words
from .scheduler import format_run, next_occurrence
from .store import TOGGLES

LOG = logging.getLogger(__name__)
GROUP_TYPES = {"group", "supergroup"}
ADMIN_STATUSES = {"creator", "administrator"}
RESTRICTED = {
    "can_send_messages": False, "can_send_audios": False,
    "can_send_documents": False, "can_send_photos": False,
    "can_send_videos": False, "can_send_video_notes": False,
    "can_send_voice_notes": False, "can_send_polls": False,
    "can_send_other_messages": False, "can_add_web_page_previews": False,
}
UNRESTRICTED = {key: True for key in RESTRICTED}
MENU_TOGGLES = (
    ("🔗 链接拦截", "block_links"),
    ("🔁 禁止转发", "block_forward"),
    ("🚦 防刷屏", "anti_flood"),
    ("👋 欢迎消息", "welcome_enabled"),
    ("🧩 新人验证", "verify_enabled"),
    ("🤖 拦截新入群机器人", "block_new_bots"),
    ("🪙 签到积分", "points_enabled"),
    ("🖼️ 重复图片拦截", "repeat_photo_enabled"),
)


def admin_status(member):
    return isinstance(member, dict) and member.get("status") in ADMIN_STATUSES


class GroupBot:
    def __init__(self, api, store, bot_id, username, owner_ids=frozenset()):
        self.api = api
        self.store = store
        self.bot_id = int(bot_id)
        self.username = username
        self.owner_ids = owner_ids
        self.flood = FloodGuard()
        self.photo_guard = PhotoRepeatGuard()
        self.economy = Economy(store)
        self.raffles = Raffles(store)
        self.ocr = OCRWorker(
            api, enabled=os.getenv("OCR_ENABLED", "0").lower() in
            ("true", "yes", "on", "1"))
        self._ocr_invalidated = {}
        self._admins = {}  # chat_id -> (expiration, frozenset of ids)
        self._reply_time = defaultdict(float)
        self._public_command_time = {}

    def send(self, chat_id, text, **kwargs):
        return self.api.safe("sendMessage", chat_id=chat_id, text=str(text)[:4000], **kwargs)

    def answer(self, query_id, text="", alert=False):
        self.api.safe("answerCallbackQuery", callback_query_id=query_id,
                      text=str(text)[:190], show_alert=alert)

    def log(self, chat_id, action, actor=None, target=None):
        # Logging is best effort and intentionally never shows moderation in the group.
        conf = self.store.settings(chat_id)
        destination = conf["log_chat_id"]
        if not destination:
            return
        summary = f"🛡️ 群组 {chat_id} | {str(action)[:120]}"
        if actor:
            summary += f" | 操作者 {actor}"
        if target:
            summary += f" | 用户 {target}"
        self.send(destination, summary)

    def _get_admins(self, chat_id):
        now = time.monotonic()
        cached = self._admins.get(chat_id)
        if cached and cached[0] > now:
            return cached[1]
        try:
            admins = self.api.call("getChatAdministrators", chat_id=chat_id)
        except TelegramAPIError as exc:
            LOG.warning("无法读取管理员名单（跳过本次自动过滤）: %s", exc)
            return None
        ids = frozenset(int(m["user"]["id"]) for m in admins if m.get("user", {}).get("id"))
        self._admins[chat_id] = (now + 60, ids)
        return ids

    def is_admin(self, chat_id, user_id):
        """Fresh check for every dangerous command or private callback."""
        if not user_id:
            return False
        try:
            return admin_status(self.api.call(
                "getChatMember", chat_id=chat_id, user_id=int(user_id)))
        except TelegramAPIError:
            return False

    def bot_can(self, chat_id, permission):
        try:
            member = self.api.call("getChatMember", chat_id=chat_id, user_id=self.bot_id)
        except TelegramAPIError:
            return False
        return member.get("status") == "creator" or (
            member.get("status") == "administrator" and member.get(permission, False))

    def is_supergroup(self, chat_id):
        try:
            info = self.api.call("getChat", chat_id=chat_id)
            return isinstance(info, dict) and info.get("type") == "supergroup"
        except TelegramAPIError:
            return False

    def can_target(self, chat_id, user_id):
        if not user_id or int(user_id) == self.bot_id:
            return False
        try:
            status = self.api.call("getChatMember", chat_id=chat_id, user_id=int(user_id))
        except TelegramAPIError:
            return False
        return not admin_status(status)

    def unrestrict(self, chat_id, user_id):
        # Telegram still applies the group's default permissions. Preserve
        # explicitly disabled default rights rather than granting all rights.
        permissions = UNRESTRICTED.copy()
        try:
            full = self.api.call("getChat", chat_id=chat_id)
            group_defaults = full.get("permissions", {}) if isinstance(full, dict) else {}
            for key in permissions:
                if key in group_defaults:
                    permissions[key] = bool(group_defaults[key])
        except TelegramAPIError:
            # Fail closed: don't grant broad permissions when defaults are unknown.
            return False
        return self.api.safe("restrictChatMember", chat_id=chat_id,
                             user_id=user_id, permissions=permissions) is True

    def _button(self, label, data):
        return {"text": label, "callback_data": data}

    def _edit_or_send(self, chat_id, message_id, text, keyboard=None):
        kwargs = {"reply_markup": {"inline_keyboard": keyboard}} if keyboard else {}
        if message_id:
            updated = self.api.safe("editMessageText", chat_id=chat_id,
                                    message_id=message_id, text=text, **kwargs)
            if updated:
                return updated
        return self.send(chat_id, text, **kwargs)

    def _private_groups(self, user_id, message_id=None):
        keyboard = []
        for chat in self.store.chats()[:30]:
            if self.is_admin(chat["chat_id"], user_id):
                keyboard.append([self._button(chat["title"][:35], f"g:{chat['chat_id']}")])
            if len(keyboard) >= 12:
                break
        if not keyboard:
            return self._edit_or_send(
                user_id, message_id, "暂未发现你有权管理的群组。\n"
                "请先将机器人加入群、授予管理权限，并在群里发送 /panel。")
        return self._edit_or_send(
            user_id, message_id, "📋 选择要管理的群组：", keyboard)

    def _private_panel(self, user_id, chat_id, message_id=None):
        info = self.store.chat(chat_id)
        if not info or not self.is_admin(chat_id, user_id):
            return self._edit_or_send(user_id, message_id, "没有权限管理这个群组。")
        settings = self.store.settings(chat_id)
        keyboard = []
        for label, key in MENU_TOGGLES:
            keyboard.append([self._button(
                f"{label}：{'开' if settings[key] else '关'}", f"t:{chat_id}:{key}")])
        keyboard.extend([
            [self._button("⛔ 违禁词", f"v:{chat_id}:words"),
             self._button("🌐 域名白名单", f"v:{chat_id}:domains")],
            [self._button("💬 自动回复", f"v:{chat_id}:replies")],
            [self._button("🪙 积分与签到", f"v:{chat_id}:points"),
             self._button("🖼️ 图片防刷", f"v:{chat_id}:photo")],
            [self._button("🎁 虚拟积分商城", f"v:{chat_id}:shop"),
             self._button("🎉 免费抽奖", f"v:{chat_id}:raffle")],
            [self._button("🔎 图片文字 OCR", f"v:{chat_id}:ocr")],
            [self._button("📊 今日统计", f"v:{chat_id}:stats"),
             self._button("📈 最近7天", f"v:{chat_id}:stats7")],
            [self._button("🏆 活跃排行", f"v:{chat_id}:activity"),
             self._button("📅 定时公告", f"v:{chat_id}:schedules")],
            [self._button("📖 命令指南", f"v:{chat_id}:help")],
            [self._button("⬅️ 返回群组列表", "groups")],
        ])
        return self._edit_or_send(
            user_id, message_id,
            f"⚙️ {info['title']}\n群组 ID：{chat_id}\n"
            "所有设置仅对本群生效。处罚默认静默，可指定私有日志群。",
            keyboard)

    def _private_view(self, user_id, chat_id, what, message_id):
        if not self.is_admin(chat_id, user_id):
            return self._edit_or_send(user_id, message_id, "没有权限。")
        keyboard = []
        if what == "words":
            words = [r["value"] for r in self.store.filters(chat_id, "word")]
            text = "⛔ 违禁词：\n" + ("\n".join(words[:60]) if words else "暂无")
            text += "\n\n在群内用 /addword 词语 添加，用 /delword 词语 删除。"
        elif what == "domains":
            domains = [r["value"] for r in self.store.filters(chat_id, "domain")]
            text = "🌐 允许的域名：\n" + ("\n".join(domains[:60]) if domains else "暂无")
            text += "\n\n在群内用 /allowdomain example.com 添加。"
        elif what == "stats":
            data = self.store.stats(chat_id)
            text = ("📊 今日统计\n"
                    f"消息 {data.get('messages', 0)} 条\n"
                    f"删除 {data.get('deleted', 0)} 条\n"
                    f"入群 {data.get('joins', 0)} 人\n"
                    f"处罚 {data.get('actions', 0)} 次")
        elif what == "replies":
            entries = self.store.replies(chat_id)
            text = "💬 本群自动回复：\n" + (
                "\n".join(f"{'🖼️' if entry['media_type'] else '💬'} {entry['keyword']}"
                          for entry in entries[:40]) or "暂无")
            text += "\n\n添加：/addreply 词1,词2 | 回复文字 | 按钮 - https://example.com"
            text += "\n可回复已有图片或视频并发送添加命令，复用媒体。"
        elif what == "points":
            conf = self.store.settings(chat_id)
            ranking = self.store.point_top(chat_id, 8)
            text = (
                f"🪙 签到积分：{'开启' if conf['points_enabled'] else '关闭'}\n"
                f"基础奖励：{conf['checkin_award']} 分；连续签到最多加 6 分\n"
                "排行（用户ID / 积分）：\n"
                + ("\n".join(
                    f"{i}. {r['user_id']} — {r['balance']}"
                    for i, r in enumerate(ranking, 1)) or "暂无记录")
                + "\n\n管理员：/pointmode on|off、/pointrate 5"
                  "\n回复成员 /pointadd 20 或 /pointtake 20"
                  "\n所有成员：/checkin、/points、/pointtop"
            )
        elif what == "photo":
            conf = self.store.settings(chat_id)
            text = (
                f"🖼️ 重复图片拦截：{'开启' if conf['repeat_photo_enabled'] else '关闭'}\n"
                f"相同图片：{conf['repeat_photo_window']} 秒内连续"
                f"{conf['repeat_photo_limit']} 次触发\n"
                "只针对同一群同一成员的 Telegram 图片 file_unique_id。\n"
                "不能识别单张广告图片、二维码或重新压缩后的图片。\n"
                "\n/imageflood on|off\n/imagelimit 3\n/imagewindow 60"
            )
        elif what == "shop":
            items = self.economy.items(chat_id)
            pending = self.economy.orders(chat_id, status="pending", limit=40)
            text = ("🎁 群内虚拟奖励商城\n"
                    f"等待人工核销：{len(pending)} 笔（最多显示前40）\n"
                    + ("\n".join(
                        f"#{item['id']} {item['title']} · {item['price']} 分 · 余{item['stock']}"
                        for item in items[:18]) or "暂无上架商品")
                    + "\n\n/shopadd 名称 | 单价 | 库存 | 说明\n"
                      "/shoporders /shopdone 编号 /shoprefund 编号\n"
                      "成员：/shop /buy 编号 /myorders")
        elif what == "raffle":
            raffle = self.raffles.active(chat_id)
            history = self.raffles.history(chat_id, limit=5)
            text = "🎉 免费群抽奖（不收费、不消耗积分）\n"
            text += (f"正在开放 #{raffle['id']} {raffle['title']}\n"
                     if raffle else "当前没有开放活动\n")
            text += ("\n".join(f"#{r['id']} {r['title']} [{r['status']}]"
                               for r in history) or "暂无历史")
            text += ("\n\n/rafflestart 标题 /raffledraw /rafflecancel\n"
                     "成员：/raffle /joinraffle")
        elif what == "ocr":
            settings = self.store.settings(chat_id)
            text = (
                f"🔎 图片文字 OCR 模式：{settings['ocr_mode']}\n"
                f"引擎：{'已安装并启用' if self.ocr.available else '未启用/未安装'}\n"
                f"最低可信度：{settings['ocr_confidence']}\n"
                "仅扫描普通成员的 Telegram 照片，使用本机 Tesseract。\n"
                "命中至少 4 字的违禁词才记录或处理，不识别二维码。\n\n"
                "/ocr review 审核日志（推荐）\n"
                "/ocr delete 高置信自动删除（误判风险）\n"
                "/ocr off 关闭 /ocrconfidence 70 设置可信度\n"
                "OCR_ENABLED=1 且容器安装 Tesseract 才能运行。"
            )
        elif what == "stats7":
            data = self.store.stats_days(chat_id, 7)
            text = ("📈 最近7天（包含今天）\n"
                    f"消息 {data.get('messages', 0)} 条\n"
                    f"删除 {data.get('deleted', 0)} 条\n"
                    f"入群 {data.get('joins', 0)} 人\n"
                    f"处罚 {data.get('actions', 0)} 次")
        elif what == "activity":
            rows = self.store.top_activity(chat_id, days=7)
            text = "🏆 最近7天活跃排行（仅记录用户 ID）\n" + (
                "\n".join(f"{i}. {r['user_id']}：{r['count']} 条"
                          for i, r in enumerate(rows, 1)) or "暂无非命令消息")
            text += "\n历史明细最多保留 30 天。"
        elif what == "schedules":
            tasks = self.store.schedules(chat_id)
            text = "📅 定时公告\n" + (
                "\n".join(f"#{r['id']} {r['title'][:25]} [{r['mode']}]"
                          f" {'开启' if r['enabled'] else '暂停/结束'}"
                          for r in tasks) or "暂无定时任务")
            text += "\n\n在群内使用 /schedule 查看新增、暂停和删除的详细用法。"
            for r in tasks[:15]:
                keyboard.append([
                    self._button(f"#{r['id']} {r['title'][:30]}", f"s:{chat_id}:{r['id']}")
                ])
        elif what == "help":
            text = self.help_text()
        else:
            text = "该功能暂不可用。"
        keyboard.append([self._button("⬅️ 返回设置", f"g:{chat_id}")])
        return self._edit_or_send(
            user_id, message_id, text, keyboard)

    def _private_schedule(self, user_id, chat_id, task_id, action, message_id, query_id):
        item = self.store.schedule(chat_id, task_id)
        if not item:
            self.answer(query_id, "任务不存在或已经删除。", alert=True)
            return self._private_view(user_id, chat_id, "schedules", message_id)
        if action == "sx":
            self.store.delete_schedule(chat_id, task_id)
            self.log(chat_id, f"删除定时公告 #{task_id}", actor=user_id)
            self.answer(query_id, "任务已删除")
            return self._private_view(user_id, chat_id, "schedules", message_id)
        if action == "sp":
            enabled = not bool(item["enabled"])
            if enabled:
                try:
                    next_run = next_occurrence(
                        item["mode"], item["rule"], self.store.tz)
                except ValueError as exc:
                    self.answer(query_id, str(exc)[:190], alert=True)
                    return
            else:
                next_run = None
            self.store.toggle_schedule(chat_id, task_id, enabled, next_run=next_run)
            self.log(chat_id, f"定时公告 #{task_id} {'开启' if enabled else '暂停'}",
                     actor=user_id)
            self.answer(query_id, "操作已保存")
            item = self.store.schedule(chat_id, task_id)
        else:
            self.answer(query_id)
        text = (
            f"📅 定时公告 #{item['id']}\n标题：{item['title']}\n"
            f"发送频率：{item['mode']} {item['rule']}\n"
            f"当前状态：{'运行中' if item['enabled'] else '暂停/结束'}\n"
            f"下次计划：{format_run(item['next_run'], self.store.tz)}\n"
            f"内容：\n{item['body'][:1400]}")
        keyboard = []
        if action == "sd":
            text += "\n\n⚠️ 确认永久删除？"
            keyboard.append([
                self._button("✅ 确认删除", f"sx:{chat_id}:{task_id}"),
                self._button("❌ 取消", f"s:{chat_id}:{task_id}"),
            ])
        else:
            keyboard.append([
                self._button("▶️ 恢复" if not item["enabled"] else "⏸ 暂停",
                             f"sp:{chat_id}:{task_id}"),
                self._button("🗑 删除", f"sd:{chat_id}:{task_id}"),
            ])
        keyboard.append([self._button("⬅️ 返回公告列表", f"v:{chat_id}:schedules")])
        return self._edit_or_send(user_id, message_id, text, keyboard)

    @staticmethod
    def help_text():
        return (
            "🤖 中文群管命令（在群内使用）\n"
            "/panel 私聊管理面板 /settings 查看开关\n"
            "/links on|off /forward on|off /flood on|off\n"
            "/captcha on|off /welcome on|off\n"
            "/addword 词 /delword 词 /words\n"
            "/allowdomain 域名 /deldomain 域名 /domains\n"
            "/setrules 群规文字 /rules 查看群规\n"
            "/setwelcome 欢迎语（{name} 自动替换）\n"
            "/addreply 词1,词2 | 回复 | 按钮 - https://网址\n"
            "/delreply 关键词 /replies；可回复图片/视频添加媒体\n"
            "/setlog -100... 设置日志群；/setlog off 关闭\n"
            "/stats 今日统计 /statweek 最近7天 /top7 活跃排行\n"
            "/schedule 定时公告管理 /bots on|off 防新机器人\n"
            "/checkin 签到 /points 我的积分 /pointtop 排行榜\n"
            "/transfer 20 回复同群成员，点击确认后转积分\n"
            "/shop 查看虚拟奖励 /buy 编号 /myorders\n"
            "/shopadd 名称 | 价格 | 库存 | 说明（管理员）\n"
            "/shoporders /shopdone 编号 /shoprefund 编号\n"
            "/raffle /joinraffle 免费参与\n"
            "/rafflestart 标题 /raffledraw /rafflecancel（管理员）\n"
            "/ocr review|delete|off /ocrconfidence 70 图片审核\n"
            "/pointmode on|off /pointrate 5 管理签到\n"
            "回复成员 /pointadd 20、/pointtake 20；/pointlog\n"
            "/imageflood on|off /imagelimit 3 /imagewindow 60\n"
            "/check 检查权限 /id 查看群 ID\n"
            "回复成员消息：/warn、/unwarn、/mute 60、/unmute、"
            "/ban、/kick、/dban；/unban 用户ID\n"
            "群内不公开播报成功的封禁，管理员发的管理命令不会自动删除。")

    def _private_message(self, message):
        sender = message.get("from", {})
        uid = sender.get("id")
        text = (message.get("text") or "").strip()
        if not uid:
            return
        cmd = text.split(maxsplit=1)[0].split("@")[0].lower()
        if cmd in ("/start", "/groups", "/panel"):
            self._private_groups(uid)
        elif cmd in ("/help", "/commands"):
            self.send(uid, self.help_text())
        elif cmd == "/health" and uid in self.owner_ids:
            self.send(uid, "✅ 数据库连接正常，机器人运行中。")
        else:
            self.send(uid, "请发送 /start 打开中文群管理面板，或 /help 查看命令。")

    def _transfer_callback(self, query):
        data = query.get("data", "")
        parts = data.split(":")
        qid = query.get("id")
        uid = (query.get("from") or {}).get("id")
        message = query.get("message") or {}
        group = message.get("chat") or {}
        if len(parts) != 4 or not uid or not qid:
            return
        try:
            cid, transfer_id = int(parts[1]), int(parts[2])
        except ValueError:
            return self.answer(qid, "转账按钮格式不正确", alert=True)
        if group.get("id") != cid or group.get("type") not in GROUP_TYPES:
            return self.answer(qid, "只能在发起转账的原群操作", alert=True)
        intent = self.economy.get_transfer(cid, transfer_id)
        if not intent or intent["source_id"] != uid or intent["nonce"] != parts[3]:
            return self.answer(qid, "你不是转账发起者，或按钮已失效", alert=True)
        if parts[0] == "et" and intent["status"] == "pending":
            # A removed/blocked member must not transfer after leaving a chat.
            try:
                left = self.api.call("getChatMember", chat_id=cid, user_id=uid)
                right = self.api.call(
                    "getChatMember", chat_id=cid, user_id=intent["target_id"])
            except TelegramAPIError:
                return self.answer(qid, "暂时无法核验群成员身份，请重试", alert=True)
            permitted = {"creator", "administrator", "member", "restricted"}
            if left.get("status") not in permitted or right.get("status") not in permitted:
                return self.answer(qid, "有成员已经离开本群，转账不能执行", alert=True)
        try:
            result = self.economy.confirm_transfer(
                cid, transfer_id, parts[3], uid, message.get("message_id"),
                accept=parts[0] == "et")
        except ValueError as exc:
            return self.answer(qid, str(exc)[:185], alert=True)
        label = {
            "executed": "✅ 转账成功",
            "cancelled": "❌ 转账已取消",
            "expired": "⏳ 确认超时，未扣除积分",
        }.get(result["status"], "转账已处理")
        self.answer(qid, label)
        if result["created"] or result["status"] == "expired":
            self.api.safe(
                "editMessageText", chat_id=cid,
                message_id=message["message_id"],
                text=f"{label}\n发起人：{uid}\n积分变动仅限群内虚拟积分。")
        if result["created"] and result["status"] == "executed":
            self.log(cid, f"确认积分转赠 {result['amount']} 分",
                     actor=uid, target=result["target_id"])

    def _callback(self, query):
        data = query.get("data", "")
        qid = query.get("id")
        sender = query.get("from", {})
        user_id = sender.get("id")
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        if not user_id or not qid:
            return
        if data.startswith(("et:", "ec:")):
            return self._transfer_callback(query)
        if data.startswith("verify:"):
            self._verify(query)
            return
        # Management callbacks work only in the requesting user's private chat.
        if chat.get("type") != "private" or chat.get("id") != user_id:
            self.answer(qid, "请私聊机器人操作。", alert=True)
            return
        if data == "groups":
            self.answer(qid)
            return self._private_groups(user_id, message.get("message_id"))
        parts = data.split(":")
        if len(parts) not in (2, 3) or parts[0] not in (
            "g", "t", "v", "s", "sd", "sp", "sx"):
            self.answer(qid, "未知或失效的按钮")
            return
        try:
            chat_id = int(parts[1])
        except ValueError:
            self.answer(qid, "按钮参数无效")
            return
        if not self.store.chat(chat_id) or not self.is_admin(chat_id, user_id):
            self.answer(qid, "你不是该群管理员。", alert=True)
            return
        if parts[0] == "g" and len(parts) == 2:
            self.answer(qid)
            return self._private_panel(user_id, chat_id, message.get("message_id"))
        if len(parts) != 3:
            self.answer(qid, "按钮参数无效")
            return
        action = parts[2]
        if parts[0] == "v":
            self.answer(qid)
            return self._private_view(user_id, chat_id, action, message.get("message_id"))
        if parts[0] in ("s", "sd", "sp", "sx"):
            try:
                task_id = int(action)
            except ValueError:
                self.answer(qid, "任务编号无效", alert=True)
                return
            return self._private_schedule(user_id, chat_id, task_id,
                                          parts[0], message.get("message_id"), qid)
        if action not in TOGGLES:
            self.answer(qid, "不允许修改这个设置。", alert=True)
            return
        needed = ("can_restrict_members" if action in ("verify_enabled", "block_new_bots")
                  else "can_delete_messages")
        if action not in ("verify_enabled", "block_new_bots",
                          "block_links", "block_forward", "anti_flood",
                          "repeat_photo_enabled"):
            needed = None
        current = self.store.settings(chat_id)[action]
        if needed and not current and not self.bot_can(chat_id, needed):
            self.answer(qid, "机器人缺少必要管理员权限，请先授予权限。", alert=True)
            return
        if (action in ("verify_enabled", "block_new_bots") and not current
                and not self.is_supergroup(chat_id)):
            self.answer(qid, "该功能仅支持超级群组。", alert=True)
            return
        self.store.set(chat_id, action, not current)
        self.answer(qid, "设置已保存")
        self._private_panel(user_id, chat_id, message.get("message_id"))
        self.log(chat_id, f"开关 {action} → {not current}", actor=user_id)

    def _verify(self, query):
        qid = query["id"]
        user_id = query["from"]["id"]
        parts = query.get("data", "").split(":")
        if len(parts) != 5:
            return self.answer(qid, "验证按钮无效。", alert=True)
        try:
            chat_id, intended, nonce, chosen = (int(parts[1]), int(parts[2]),
                                                parts[3], int(parts[4]))
        except (ValueError, IndexError):
            return self.answer(qid, "验证参数错误。", alert=True)
        if user_id != intended:
            return self.answer(qid, "只能由需要验证的新成员点击。", alert=True)
        # Bind callback to the original group and message to block replay.
        message = query.get("message") or {}
        if (message.get("chat") or {}).get("id") != chat_id:
            return self.answer(qid, "验证来源不匹配。", alert=True)
        pending = self.store.pending(chat_id, user_id)
        if not pending or pending["nonce"] != nonce or pending["message_id"] != message.get("message_id"):
            return self.answer(qid, "验证已失效。", alert=True)
        if pending["expires_at"] <= time.time():
            return self.answer(qid, "验证已过期，请联系管理员。", alert=True)
        if chosen != pending["answer"]:
            return self.answer(qid, "答案不正确，请重试。", alert=True)
        if not self.unrestrict(chat_id, user_id):
            return self.answer(qid, "机器人缺少解除限制的权限，请联系管理员。", alert=True)
        self.store.del_pending(chat_id, user_id)
        self.api.safe("deleteMessage", chat_id=chat_id, message_id=message["message_id"])
        self.answer(qid, "✅ 验证成功，欢迎加入！")
        self.log(chat_id, "新人验证成功", target=user_id)

    def _new_members(self, message):
        chat = message["chat"]
        chat_id = chat["id"]
        settings = self.store.settings(chat_id)
        for member in message.get("new_chat_members", []):
            uid = member.get("id")
            if uid is None or uid == self.bot_id or member.get("is_bot"):
                if (uid and uid != self.bot_id and member.get("is_bot")
                    and settings["block_new_bots"]
                    and self.bot_can(chat_id, "can_restrict_members")):
                    removed = self.api.safe("banChatMember", chat_id=chat_id,
                                            user_id=uid)
                    if removed is True:
                        self.api.safe("unbanChatMember", chat_id=chat_id,
                                      user_id=uid, only_if_banned=True)
                        self.log(chat_id, "拦截新加入的第三方机器人", target=uid)
                continue
            self.store.stat(chat_id, "joins")
            if settings["verify_enabled"]:
                self._start_verification(chat_id, member, settings)
            elif settings["welcome_enabled"]:
                name = str(member.get("first_name", "新朋友"))[:64]
                self.send(chat_id, settings["welcome_text"].replace("{name}", name))

    def _start_verification(self, chat_id, member, settings):
        uid = member["id"]
        now = int(time.time())
        timeout = settings["verify_timeout"]
        if not self.bot_can(chat_id, "can_restrict_members"):
            self.log(chat_id, "新人验证未启用：缺少限制成员权限", target=uid)
            return
        # Keep the member muted until verification succeeds or timeout cleanup.
        # A timed Telegram restriction would expire before offline cleanup can kick.
        restricted = self.api.safe("restrictChatMember", chat_id=chat_id, user_id=uid,
                                   permissions=RESTRICTED, until_date=0)
        if restricted is not True:
            self.log(chat_id, "无法限制新成员，跳过验证", target=uid)
            return
        a, b = random.randint(2, 9), random.randint(1, 9)
        correct = a + b
        options = {correct}
        while len(options) < 4:
            option = max(1, correct + random.choice((-5, -4, -3, -2, 2, 3, 4, 5)))
            options.add(option)
        options = list(options)
        random.shuffle(options)
        nonce = secrets.token_hex(4)
        expiry = now + timeout
        self.store.put_pending(chat_id, uid, nonce, options.index(correct), expiry)
        buttons = [[self._button(
            str(choice), f"verify:{chat_id}:{uid}:{nonce}:{index}")
            for index, choice in enumerate(options[:2])],
            [self._button(
                str(choice), f"verify:{chat_id}:{uid}:{nonce}:{index}")
             for index, choice in enumerate(options[2:], start=2)]]
        name = str(member.get("first_name", "新成员"))[:64]
        posted = self.send(chat_id, f"🧩 {name}，欢迎加入！请在 {timeout // 60} 分钟内回答："
                           f"{a} + {b} = ？（仅本人可点击）",
                           reply_markup={"inline_keyboard": buttons})
        if not isinstance(posted, dict) or not posted.get("message_id"):
            # Don't trap a legitimate member if the prompt cannot be sent.
            self.unrestrict(chat_id, uid)
            self.store.del_pending(chat_id, uid)
            self.log(chat_id, "验证题发送失败，尝试解除限制", target=uid)
            return
        self.store.put_pending(chat_id, uid, nonce, options.index(correct),
                               expiry, posted["message_id"])

    def cleanup_expired(self):
        # Keep failed expirations queued for retry if bot permissions are broken.
        for pending in self.store.expired():
            chat_id, uid = pending["chat_id"], pending["user_id"]
            try:
                member = self.api.call("getChatMember", chat_id=chat_id, user_id=uid)
            except TelegramAPIError:
                continue
            if member.get("status") in ("left", "kicked") or admin_status(member):
                completed = True
            elif self.bot_can(chat_id, "can_restrict_members"):
                completed = self.api.safe("banChatMember", chat_id=chat_id, user_id=uid) is True
                if completed:
                    # Unban so the member may retry joining; do not auto-reinvite.
                    self.api.safe("unbanChatMember", chat_id=chat_id, user_id=uid,
                                  only_if_banned=True)
                    self.log(chat_id, "新人验证超时，已移出群（允许重新加入）", target=uid)
            else:
                completed = False
            if completed:
                if pending.get("message_id"):
                    self.api.safe("deleteMessage", chat_id=chat_id,
                                  message_id=pending["message_id"])
                self.store.del_pending(chat_id, uid)

    def _moderate(self, message, *, is_edit=False):
        chat = message["chat"]
        chat_id = chat["id"]
        sender = message.get("from") or {}
        user_id = sender.get("id")
        if not user_id or sender.get("is_bot"):
            return
        # An anonymous administrator sends on behalf of this group.
        if (message.get("sender_chat") or {}).get("id") == chat_id:
            return
        admin_ids = self._get_admins(chat_id)
        if admin_ids is None:  # Fail open rather than deleting admin messages.
            return
        if user_id in admin_ids:
            return
        settings = self.store.settings(chat_id)
        reason = evaluate(message, settings,
                          [r["key"] for r in self.store.filters(chat_id, "word")],
                          [r["key"] for r in self.store.filters(chat_id, "domain")],
                          self.flood)
        if not reason and not is_edit and settings["repeat_photo_enabled"]:
            reason = self.photo_guard.check(
                message, limit=settings["repeat_photo_limit"],
                window=settings["repeat_photo_window"])
        if reason:
            if not self.bot_can(chat_id, "can_delete_messages"):
                return False
            # Refresh membership only before deletion. An administrator promoted
            # during the short admin-list cache window must never be auto-deleted.
            try:
                fresh = self.api.call("getChatMember", chat_id=chat_id, user_id=user_id)
            except TelegramAPIError:
                # Missing membership evidence is not permission to delete.
                return False
            if admin_status(fresh):
                return True
            deleted = self.api.safe("deleteMessage", chat_id=chat_id,
                                    message_id=message["message_id"])
            if deleted is True:
                self.store.stat(chat_id, "deleted")
                self.log(chat_id, f"自动删除（{reason}）", target=user_id)
            return False
        text = message.get("text") or ""
        if not text or text.startswith("/"):
            return
        normalized = normalize(text)
        for row in self.store.replies(chat_id):
            keywords = [word for word in row["key"].split(",") if word]
            if any(word in normalized for word in keywords):
                key = (chat_id, user_id, row["key"])
                now = time.monotonic()
                if now - self._reply_time[key] < 60:
                    continue
                self._reply_time[key] = now
                if len(self._reply_time) > 20000:
                    for old, tick in list(self._reply_time.items()):
                        if now - tick > 300:
                            del self._reply_time[old]
                try:
                    buttons = json.loads(row.get("buttons") or "[]")
                except ValueError:
                    buttons = []
                kwargs = ({"reply_markup": {"inline_keyboard": [buttons]}}
                          if buttons else {})
                kind = row.get("media_type")
                if kind in ("photo", "video", "document", "animation") and row.get("media_id"):
                    self.api.safe(
                        {"photo": "sendPhoto", "video": "sendVideo",
                         "document": "sendDocument", "animation": "sendAnimation"}[kind],
                        chat_id=chat_id, caption=row["reply"][:1000],
                        **{kind: row["media_id"]}, **kwargs)
                else:
                    self.send(chat_id, row["reply"], **kwargs)
                return True
        return True

    def _maybe_queue_ocr(self, message):
        if not self.ocr.available or not message.get("photo"):
            return
        chat_id = (message.get("chat") or {}).get("id")
        source = message.get("from") or {}
        uid = source.get("id")
        if not chat_id or not uid or source.get("is_bot") or message.get("sender_chat"):
            return
        settings = self.store.settings(chat_id)
        if settings["ocr_mode"] == "off":
            return
        admins = self._get_admins(chat_id)
        if admins is None or uid in admins:
            return
        photo = message["photo"][-1]
        file_id = photo.get("file_id")
        if not file_id or (photo.get("file_size") or 0) > MAX_OCR_FILE_BYTES:
            return
        self.ocr.submit(file_id, {
            "chat_id": chat_id, "user_id": uid,
            "message_id": message.get("message_id"),
            "created_at": int(time.time()),
        })

    def drain_ocr(self):
        """Called on polling thread: SQLite and Telegram moderation are never in OCR thread."""
        for event, text, confidence in self.ocr.results():
            cid, uid = event["chat_id"], event["user_id"]
            if time.time() - event["created_at"] > 300:
                continue
            # A media edit while OCR runs must not delete the changed message.
            if (cid, event["message_id"]) in self._ocr_invalidated:
                continue
            try:
                settings = self.store.settings(cid)
            except KeyError:
                continue
            mode = settings["ocr_mode"]
            if mode == "off":
                continue
            matched = matched_words(
                text, confidence,
                [r["key"] for r in self.store.filters(cid, "word")],
                threshold=settings["ocr_confidence"])
            if not matched:
                continue
            self.store.stat(cid, "ocr_hits")
            if mode == "review":
                # No raw image, OCR text, or untrusted URL is stored in logs.
                self.log(cid, f"🔎 OCR待审核 消息 {event['message_id']}："
                         f"命中 {','.join(matched)} / 可信度 {confidence:.0f}",
                         target=uid)
                continue
            if mode != "delete" or not self.bot_can(cid, "can_delete_messages"):
                continue
            try:
                person = self.api.call("getChatMember", chat_id=cid, user_id=uid)
            except TelegramAPIError:
                continue
            if admin_status(person):
                continue
            deleted = self.api.safe(
                "deleteMessage", chat_id=cid, message_id=event["message_id"])
            if deleted is True:
                self.store.stat(cid, "deleted")
                self.store.stat(cid, "ocr_deleted")
                self.log(cid, f"🔎 OCR自动删除消息 {event['message_id']}："
                         f"命中 {','.join(matched)} / 可信度 {confidence:.0f}",
                         target=uid)

    def handle_update(self, update):
        if "channel_post" in update:
            posted = update["channel_post"]
            chat = posted.get("chat") or {}
            if chat.get("type") == "channel" and (
                posted.get("text") or "").split("@")[0] == "/id":
                self.send(chat["id"], f"🆔 频道 ID：{chat['id']}")
            return
        if "my_chat_member" in update:
            event = update["my_chat_member"]
            chat = event.get("chat") or {}
            if chat.get("type") in GROUP_TYPES and (
                event.get("new_chat_member") or {}).get("status") in ADMIN_STATUSES | {"member", "restricted"}:
                self.store.ensure_chat(chat["id"], chat.get("title"))
            return
        if "callback_query" in update:
            return self._callback(update["callback_query"])
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        chat = message.get("chat") or {}
        if chat.get("type") == "private":
            if "edited_message" not in update:
                return self._private_message(message)
            return
        if chat.get("type") not in GROUP_TYPES:
            return
        chat_id = chat["id"]
        self.store.ensure_chat(chat_id, chat.get("title"))
        if "edited_message" in update and message.get("message_id"):
            self._ocr_invalidated[(chat_id, message["message_id"])] = time.monotonic()
            if len(self._ocr_invalidated) > 3000:
                cutoff = time.monotonic() - 360
                for key, last_seen in list(self._ocr_invalidated.items()):
                    if last_seen < cutoff:
                        del self._ocr_invalidated[key]
        if "edited_message" not in update:
            self.store.stat(chat_id, "messages")
        if message.get("new_chat_members"):
            return self._new_members(message)
        if message.get("left_chat_member"):
            left = message["left_chat_member"].get("id")
            if left:
                self.raffles.drop_member(chat_id, left)
                previous = self.store.pending(chat_id, left)
                if previous and previous.get("message_id"):
                    self.api.safe("deleteMessage", chat_id=chat_id,
                                  message_id=previous["message_id"])
                self.store.del_pending(chat_id, left)
            return
        if "edited_message" not in update:
            from .commands import handle_command
            if handle_command(self, message):
                return
        allowed = self._moderate(message, is_edit="edited_message" in update)
        if "edited_message" not in update and allowed is not False:
            self._maybe_queue_ocr(message)
        if "edited_message" not in update and allowed is not False:
            sender = message.get("from") or {}
            if (sender.get("id") and not sender.get("is_bot")
                and not message.get("sender_chat")
                and not (message.get("text") or "").startswith("/")
                and (message.get("text") or message.get("caption"))):
                self.store.record_activity(chat_id, sender["id"])
