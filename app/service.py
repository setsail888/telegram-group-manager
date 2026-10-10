"""Telegram event routing, authorization, private settings and join challenge."""
import json
import logging
import os
import random
import re
import secrets
import time
from collections import deque

from .api import TelegramAPIError
from .economy import Economy
from .raffle import Raffles
from .moderation import FloodGuard, PhotoRepeatGuard, evaluate, normalize, match_words
from .ocr import OCRWorker, MAX_OCR_FILE_BYTES, matched_words
from .scheduler import format_run, next_occurrence
from .store import TOGGLES, NUMBER_LIMITS
from .admin_ui import AdminUI
from .member_ui import MemberUI

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
# Like the reference Telegram bot: compact two-column module navigation.
# Display only implemented modules; every button leads to a working page.
MENU_MODULES = (
    ("🎉 免费抽奖", "raffle"), ("🔗 链接管理", "links"),
    ("🛡️ 智能反垃圾", "spam"), ("🚦 防刷屏", "flood"),
    ("⛔ 违禁词", "words"), ("🧩 新人验证", "verify"),
    ("👤 新成员限制", "members"), ("👋 欢迎消息", "welcome"),
    ("🤖 机器人防护", "bots"), ("🔁 禁止转发", "forward"),
    ("🖼️ 图片防刷", "photo"), ("🔎 图片 OCR", "ocr"),
    ("🌐 域名白名单", "domains"), ("💬 自动回复", "replies"),
    ("📅 定时公告", "schedules"), ("🪙 积分签到", "points"),
    ("🎁 积分商城", "shop"), ("📊 今日统计", "stats"),
    ("📈 最近7天", "stats7"), ("🏆 活跃排行", "activity"),
    ("⚙️ 规则总览", "overview"), ("📖 命令指南", "help"),
    ("🔍 防护记录", "audit"), ("👮 成员管理", "manage"),
    ("🧾 管理日志", "logs"), ("🔄 切换群组", "groups"),
)
PAGE_SWITCHES = {
    "spam": (("🛡️ 智能反垃圾", "spam_guard_enabled"),
             ("🔗 链接拦截", "block_links"),
             ("🔁 禁止转发", "block_forward"),
             ("🔎 拆字识别", "word_loose_match"),
             ("⚖️ 自动禁言", "auto_mute_enabled"),
             ("🔍 仅观察内容过滤", "observe_only"),
             ("📣 拦截外部频道发言", "block_external_channels")),
    "flood": (("🚦 防刷屏", "anti_flood"),
              ("🖼️ 媒体连发限制", "media_flood_enabled"),
              ("📚 相册按一条计数", "album_single_enabled")),
    "floodmedia": (("🖼️ 媒体连发限制", "media_flood_enabled"),
                   ("📚 相册按一条计数", "album_single_enabled")),
    "links": (("🔗 链接拦截", "block_links"),),
    "forward": (("🔁 禁止转发", "block_forward"),),
    "words": (("🔎 拆字违禁词", "word_loose_match"),),
    "verify": (("🧩 新人验证", "verify_enabled"),),
    "welcome": (("👋 欢迎语", "welcome_enabled"),),
    "bots": (("🤖 拦截新人机器人", "block_new_bots"),),
    "members": (("👤 超长昵称限制", "name_guard_enabled"),),
    "photo": (("🖼️ 重复图片拦截", "repeat_photo_enabled"),),
    "points": (("🪙 签到积分", "points_enabled"),),
}
PAGE_NUMBERS = {
    "spam": (("mention_limit", "批量提及次数", 1),
             ("auto_mute_after", "连续违规次数", 1),
             ("auto_mute_minutes", "自动禁言分钟", 5)),
    "flood": (("flood_limit", "短时消息条数", 1),
              ("flood_window", "刷屏窗口（秒）", 5),
              ("repeat_limit", "重复消息次数", 1),
              ("repeat_window", "重复判断（秒）", 5),
              ("media_flood_limit", "媒体连发条数", 1),
              ("media_flood_window", "媒体窗口（秒）", 5)),
    "floodcount": (("flood_limit", "触发条数", 1),),
    "floodinterval": (("flood_window", "检测间隔（秒）", 5),),
    "floodrepeat": (("repeat_limit", "重复消息次数", 1),
                    ("repeat_window", "重复判断（秒）", 5)),
    "floodmedia": (("media_flood_limit", "媒体连发条数", 1),
                   ("media_flood_window", "媒体窗口（秒）", 5)),
    "floodpenalty": (("flood_mute_minutes", "刷屏禁言分钟", 5),),
    "members": (("name_length_limit", "允许昵称最大长度", 5),),
    "wordpenalty": (("word_mute_minutes", "违禁词禁言分钟", 5),),
    "verify": (("verify_timeout", "验证时限（秒）", 60),
               ("verify_attempt_limit", "答错上限", 1)),
    "photo": (("repeat_photo_limit", "重复图片次数", 1),
              ("repeat_photo_window", "图片窗口（秒）", 30)),
    "points": (("checkin_award", "签到基础奖励", 1),),
    "ocr": (("ocr_confidence", "最低可信度", 5),),
    "replies": (("reply_group_limit", "全群每分钟回复上限", 1),),
    "manage": (("warn_limit", "警告上限", 1),),
}
WORD_OPERATIONS = {"add", "del", "except", "unexcept", "test"}
FLOOD_REASONS = frozenset(("短时刷屏", "重复刷屏", "媒体刷屏"))


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
        self._reply_time = {}
        self._public_command_time = {}
        self._word_wizards = {}  # user_id -> (chat_id, operation, expiry)
        self.admin_ui = AdminUI(self)
        self.member_ui = MemberUI(self)
        self._reply_group_time = {}
        # Rate-limit ALL public moderation reminders per group across modules.
        self._last_moderation_notice = {}

    def send(self, chat_id, text, **kwargs):
        return self.api.safe("sendMessage", chat_id=chat_id, text=str(text)[:4000], **kwargs)

    def answer(self, query_id, text="", alert=False):
        params = {"callback_query_id": query_id,
                  "text": str(text)[:190], "show_alert": alert}
        # After authorization, a routine ACK and page edit can run in
        # parallel. Important permission/error alerts remain synchronous.
        background = getattr(self.api, "answer_background", None)
        if not alert and callable(background) and background(**params):
            return
        self.api.safe("answerCallbackQuery", **params)

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

    def _private_panel(self, user_id, chat_id, message_id=None, *,
                       verified_admin=False):
        info = self.store.chat(chat_id)
        # A callback has already performed a *fresh* getChatMember call.
        # A second identical network request on the same click adds latency
        # without meaningfully improving authorization.
        if not info or (not verified_admin and not self.is_admin(chat_id, user_id)):
            return self._edit_or_send(user_id, message_id, "没有权限管理这个群组。")
        settings = self.store.settings(chat_id)
        keyboard = []
        for index in range(0, len(MENU_MODULES), 2):
            row = []
            for label, page in MENU_MODULES[index:index + 2]:
                cb = "groups" if page == "groups" else f"v:{chat_id}:{page}"
                row.append(self._button(label, cb))
            keyboard.append(row)
        active = sum(bool(settings.get(key)) for key in
                     ("block_links", "anti_flood", "block_forward",
                      "spam_guard_enabled", "media_flood_enabled",
                      "repeat_photo_enabled", "verify_enabled",
                      "name_guard_enabled"))
        return self._edit_or_send(
            user_id, message_id,
            f"⚙️ 设置「{info['title']}」\n"
            f"🛡️ 防护已开启 {active}/8 项 · 群组 {chat_id}\n"
            "请选择要更改的项目（每个群独立设置）。",
            keyboard)

    def _settings_controls(self, chat_id, page, conf):
        """Module-local controls; each callback returns to the same module."""
        keyboard = []
        switches = PAGE_SWITCHES.get(page, ())
        for i in range(0, len(switches), 2):
            row = []
            for label, key in switches[i:i + 2]:
                state = "✅" if conf[key] else "⭕"
                row.append(self._button(
                    f"{state} {label}", f"t:{chat_id}:{key}:{page}"))
            keyboard.append(row)
        for name, label, step in PAGE_NUMBERS.get(page, ()):
            keyboard.append([
                self._button(f"➖ {label}", f"n:{chat_id}:{name}:-{step}:{page}"),
                self._button(f"{conf[name]} ➕", f"n:{chat_id}:{name}:+{step}:{page}"),
            ])
        return keyboard

    def _word_navigation(self, chat_id):
        """Always give users an exit from one-shot private keyword editing."""
        return {"inline_keyboard": [[
            self._button("⬅️ 返回违禁词管理", f"v:{chat_id}:words"),
            self._button("⚙️ 返回群组设置", f"g:{chat_id}"),
        ]]}

    def _private_word_list(self, user_id, chat_id, page=0, message_id=None, *,
                           verified_admin=False):
        """Separate, bounded list; the main keyword page never shows entries."""
        if not verified_admin and not self.is_admin(chat_id, user_id):
            return self._edit_or_send(user_id, message_id, "没有权限。")
        count = self.store.filter_count(chat_id, "word")
        last_page = max(0, (count - 1) // 8)
        page = min(last_page, page)
        rows = self.store.filter_page(chat_id, "word", page, page_size=8)
        listing = "\n".join(
            f"{page * 8 + i}. {str(row['value'])[:80]}"
            for i, row in enumerate(rows, 1)) or "暂无违禁词"
        keyboard = []
        navigation = []
        if page > 0:
            navigation.append(self._button("◀️ 上一页", f"wl:{chat_id}:{page-1}"))
        if page < last_page:
            navigation.append(self._button("下一页 ▶️", f"wl:{chat_id}:{page+1}"))
        if navigation:
            keyboard.append(navigation)
        keyboard.append([self._button("⬅️ 返回违禁词管理", f"v:{chat_id}:words")])
        return self._edit_or_send(
            user_id, message_id,
            f"📋 违禁词列表 · 共 {count} 条\n"
            f"第 {page + 1}/{last_page + 1} 页\n\n{listing}",
            keyboard)

    def _private_view(self, user_id, chat_id, what, message_id, *,
                      verified_admin=False):
        if not verified_admin and not self.is_admin(chat_id, user_id):
            return self._edit_or_send(user_id, message_id, "没有权限。")
        keyboard = []
        conf = self.store.settings(chat_id)
        # The keyword page has its own compact button layout, matching the
        # reference bot, rather than an expanded rule list.
        if what not in ("words", "flood"):
            keyboard.extend(self._settings_controls(chat_id, what, conf))
        if what == "spam":
            text = (
                "🛡️ 智能反垃圾\n"
                f"链接：{'开' if conf['block_links'] else '关'} / "
                f"伪装链接与批量@：{'开' if conf['spam_guard_enabled'] else '关'}\n"
                f"批量提及：超过 {conf['mention_limit']} 个触发\n"
                f"拆字关键词：{'开' if conf['word_loose_match'] else '关'}\n"
                f"违规自动禁言：{'开' if conf['auto_mute_enabled'] else '关'}\n"
                f"累计 {conf['auto_mute_after']} 次实际删除 / 10 分钟 → "
                f"禁言 {conf['auto_mute_minutes']} 分钟\n"
                "检测文字及媒体说明中的显式@和隐藏提及；伪装链接同样遵守域名白名单。\n"
                "采用规则识别，可能漏判或误判，请先在测试群调整。\n"
                "注：自动禁言默认关闭，启用前请确认机器人限制成员权限。"
            )
        elif what == "flood":
            text = (
                "💬 反刷屏\n\n"
                f"状态：{'✅ 启用' if conf['anti_flood'] else '⭕ 关闭'}\n"
                f"当前：{conf['flood_window']} 秒 / 超过 {conf['flood_limit']} 条\n"
                f"重复：{conf['repeat_window']} 秒 / {conf['repeat_limit']} 次\n"
                f"媒体连发：{'✅ 开启' if conf['media_flood_enabled'] else '关闭'}"
                f"（{conf['media_flood_window']} 秒 / 超过 {conf['media_flood_limit']} 条）\n"
                f"惩罚：{'单次仅删除触发消息' if conf['flood_penalty'] == 'delete' else '删除并禁言 ' + str(conf['flood_mute_minutes']) + ' 分钟'}\n"
                f"删除提醒：{str(conf['flood_notice_seconds']) + ' 秒后撤回' if conf['flood_notice_seconds'] else '关闭（静默）'}"
            )
            if conf["auto_mute_enabled"] and conf["flood_penalty"] == "delete":
                text += "\n注：全局「累计违规禁言」已启用，累计达到阈值仍可能被禁言。"
            keyboard.extend([
                [self._button("✅ 启用", f"fs:{chat_id}:on"),
                 self._button("⭕ 关闭", f"fs:{chat_id}:off")],
                [self._button("💭 触发条数", f"v:{chat_id}:floodcount"),
                 self._button("⏱️ 检测间隔", f"v:{chat_id}:floodinterval")],
                [self._button("🔁 重复判断", f"v:{chat_id}:floodrepeat"),
                 self._button("🖼️ 媒体连发", f"v:{chat_id}:floodmedia")],
                [self._button("⚖️ 惩罚", f"v:{chat_id}:floodpenalty"),
                 self._button("🕒 删除提醒", f"v:{chat_id}:floodnotice")],
            ])
        elif what in ("floodcount", "floodinterval", "floodrepeat", "floodmedia"):
            labels = {
                "floodcount": "💭 触发条数",
                "floodinterval": "⏱️ 检测间隔",
                "floodrepeat": "🔁 重复消息判断",
                "floodmedia": "🖼️ 媒体连发限制",
            }
            if what == "floodmedia":
                seconds = conf["media_flood_window"]
                limit = conf["media_flood_limit"]
                enabled = conf["media_flood_enabled"] and conf["anti_flood"]
                penalty = (
                    "删除触发消息"
                    if conf["flood_penalty"] == "delete"
                    else f"删除并禁言 {conf['flood_mute_minutes']} 分钟"
                )
                notification = (
                    "静默清理，不在群内发提示"
                    if conf["flood_notice_seconds"] == 0
                    else f"发送短提示，约 {conf['flood_notice_seconds']} 秒后撤回"
                )
                text = (
                    "🖼️ 媒体连发限制\n\n"
                    f"状态：{'✅ 生效中' if enabled else '⭕ 未生效'}"
                    f"（本项{'开' if conf['media_flood_enabled'] else '关'}，"
                    f"主防刷屏{'开' if conf['anti_flood'] else '关'}）\n"
                    f"规则：同一普通成员在 {seconds} 秒内发送超过 {limit} 条媒体，"
                    f"从第 {limit + 1} 条起触发清理。\n\n"
                    "📌 计入：照片、视频、动图、贴纸、文件、语音、音频、视频留言；"
                    "不同类型合计，内容不必相同。\n"
                    "📌 不计入媒体次数：纯文字及编辑消息；管理员豁免。"
                    "中间穿插文字也不会清空该时间窗口内的媒体计数。\n\n"
                    f"例如：{seconds} 秒内已发 {limit} 条照片/视频，"
                    f"再发第 {limit + 1} 条贴纸时会删除该贴纸，"
                    "之前的媒体不会追溯删除。\n\n"
                    f"处罚：{penalty}（在「防刷屏 → 惩罚」修改）。\n"
                    f"提醒：{notification}。\n"
                    "生效条件：主防刷屏和本项两个开关都要开启，"
                    "机器人还需有删除消息权限。"
                    "纯文字仍可能触发普通刷屏规则。\n"
                    "点击下方 ➖ / ➕ 可调整条数和时间。"
                )
                if conf["auto_mute_enabled"] and conf["flood_penalty"] == "delete":
                    text += "\n⚠️ 已开启全局累计违规禁言，累计达到阈值仍可能被禁言。"
            else:
                current = (
                    f"短时消息：{conf['flood_window']} 秒内超过 {conf['flood_limit']} 条"
                    if what in ("floodcount", "floodinterval")
                    else f"重复消息：{conf['repeat_window']} 秒内达到 {conf['repeat_limit']} 次"
                )
                text = (f"{labels[what]}\n\n{current}\n\n"
                        "点击下方 ➖ / ➕ 调整，仅对当前群生效。")
            keyboard.append([self._button("⬅️ 返回防刷屏", f"v:{chat_id}:flood")])
        elif what == "floodpenalty":
            text = (
                "⚖️ 反刷屏惩罚\n\n"
                f"当前：{'仅删除触发消息' if conf['flood_penalty'] == 'delete' else '删除消息并临时禁言'}\n"
                f"禁言时长：{conf['flood_mute_minutes']} 分钟\n"
                "默认只删触发违规的消息；不回溯删除正常消息。\n"
                "临时禁言仅限超级群，需要机器人拥有删除消息和限制成员权限。\n"
                "与全局累计违规禁言不会在同一次违规中重复禁言。"
            )
            if conf["auto_mute_enabled"] and conf["flood_penalty"] == "delete":
                text += "\n⚠️ 全局累计违规禁言已开启，「单次仅删除」不代表永久免于累计处罚。"
            keyboard.extend([
                [self._button("✅ 仅删除", f"fp:{chat_id}:delete"),
                 self._button("🔇 删除并禁言", f"fp:{chat_id}:mute")],
                [self._button("⬅️ 返回防刷屏", f"v:{chat_id}:flood")],
            ])
        elif what == "floodnotice":
            text = (
                "🕒 反刷屏删除提醒\n\n"
                f"当前：{'关闭（静默）' if conf['flood_notice_seconds'] == 0 else str(conf['flood_notice_seconds']) + ' 秒后撤回'}\n"
                "只有成功删除刷屏消息才发送一条简短提醒，"
                "不包含成员身份。\n"
                "默认关闭；每群最多约30秒一次，设定时间后尽量撤回。"
            )
            keyboard.extend([
                [self._button("🔕 关闭", f"fn:{chat_id}:0"),
                 self._button("10 秒", f"fn:{chat_id}:10")],
                [self._button("30 秒", f"fn:{chat_id}:30"),
                 self._button("60 秒", f"fn:{chat_id}:60")],
                [self._button("⬅️ 返回防刷屏", f"v:{chat_id}:flood")],
            ])
        elif what == "links":
            text = (
                "🔗 链接防护\n"
                f"当前：{'拦截外链' if conf['block_links'] else '不拦截外链'}\n"
                "识别普通网址、隐藏 text_link 和常见 Telegram 链接；"
                "打开智能反垃圾后还会识别部分 t[.]me 等变体。\n"
                "域名白名单：/allowdomain example.com\n"
                "移除：/deldomain example.com"
            )
            keyboard.append([self._button("🌐 管理白名单", f"v:{chat_id}:domains")])
        elif what == "forward":
            text = "🔁 转发保护\n开启后自动删除普通成员转发的消息，管理员不受影响。"
        elif what == "verify":
            text = (
                "🧩 新人验证\n"
                f"当前：{'开启' if conf['verify_enabled'] else '关闭'}\n"
                f"验证时限：{conf['verify_timeout']} 秒\n"
                "仅超级群组可用；新人通过点击数学按钮解除限制。"
            )
        elif what == "welcome":
            text = (
                f"👋 欢迎消息：{'开启' if conf['welcome_enabled'] else '关闭'}\n"
                f"当前欢迎语：{conf['welcome_text'][:400]}\n"
                "管理员在群内：/setwelcome 欢迎 {name}！"
            )
        elif what == "bots":
            text = (
                f"🤖 拦截新入群机器人：{'开启' if conf['block_new_bots'] else '关闭'}\n"
                "仅超级群组有效；开启后也可能拦截管理员邀请的其他机器人。"
            )
        elif what == "members":
            text = (
                "👤 新成员昵称限制\n"
                f"当前：{'开启' if conf['name_guard_enabled'] else '关闭'}\n"
                f"昵称长度超过 {conf['name_length_limit']} 字时，"
                "将对普通新成员临时限制发言 30 分钟。\n"
                "默认关闭；管理员豁免，机器人需有「限制成员」权限。\n"
                "若同时开启新人验证，先执行新人验证，避免昵称限制提前解除验证禁言。\n"
                "本功能不永久封禁，适合先在测试群验证长昵称问题。"
            )
        elif what == "overview":
            text = ("⚙️ 群规则总览\n" +
                    "\n".join(f"{label}：{'开' if conf[key] else '关'}"
                              for label, key in (
                                  ("🔗 链接", "block_links"),
                                  ("🔁 转发", "block_forward"),
                                  ("🚦 刷屏", "anti_flood"),
                                  ("🛡️ 智能垃圾", "spam_guard_enabled"),
                                  ("🖼️ 媒体连发", "media_flood_enabled"),
                                  ("🔎 拆字关键词", "word_loose_match"),
                                  ("⚖️ 自动禁言", "auto_mute_enabled"),
                                  ("🧩 验证", "verify_enabled"))) +
                    "\n\n设置均仅作用于当前群。")
        elif what == "words":
            words_total = self.store.filter_count(chat_id, "word")
            exceptions_total = self.store.filter_count(chat_id, "word_exception")
            enabled = conf["word_filter_enabled"]
            punishment = ("仅删除消息" if conf["word_penalty"] == "delete"
                          else f"删除并禁言 {conf['word_mute_minutes']} 分钟")
            reminder = (f"{conf['word_notice_seconds']} 秒后撤回"
                        if conf["word_notice_seconds"] else "关闭（静默）")
            text = (
                "⛔ 违禁词\n\n"
                f"状态：{'✅ 启用' if enabled else '⭕ 关闭'}\n"
                f"惩罚：{punishment}\n"
                f"删除提醒：{reminder}\n"
                f"规则：{words_total} 条 · 精确例外：{exceptions_total} 条"
            )
            keyboard.extend([
                [self._button("✅ 启用", f"ws:{chat_id}:on"),
                 self._button("⭕ 关闭", f"ws:{chat_id}:off")],
                [self._button("➕ 添加违禁词", f"w:{chat_id}:add"),
                 self._button("➖ 删除违禁词", f"w:{chat_id}:del")],
                [self._button("📋 违禁词列表", f"wl:{chat_id}:0")],
                [self._button("⚖️ 惩罚", f"v:{chat_id}:wordpenalty"),
                 self._button("🕒 删除提醒", f"v:{chat_id}:wordnotice")],
                [self._button(
                    f"{'✅' if conf['word_loose_match'] else '⭕'} 🔎 拆字违禁词",
                    f"t:{chat_id}:word_loose_match:words")],
                [self._button("🛡️ 添加精确例外", f"w:{chat_id}:except"),
                 self._button("❌ 删除精确例外", f"w:{chat_id}:unexcept")],
                [self._button("🧪 测试关键词匹配", f"w:{chat_id}:test")],
            ])
        elif what == "wordpenalty":
            text = (
                "⚖️ 违禁词处罚\n\n"
                f"当前：{'仅删除消息' if conf['word_penalty'] == 'delete' else '删除消息并临时禁言'}\n"
                f"禁言时长：{conf['word_mute_minutes']} 分钟\n"
                "仅处理命中违禁词的普通群成员；默认仅删除，保持静默。\n"
                "启用禁言需要机器人具有「限制成员」权限，并且群是超级群。"
            )
            keyboard.extend([
                [self._button("✅ 仅删除", f"wp:{chat_id}:delete"),
                 self._button("🔇 删除并禁言", f"wp:{chat_id}:mute")],
                [self._button("➖ 5 分钟", f"n:{chat_id}:word_mute_minutes:-5:wordpenalty"),
                 self._button("➕ 5 分钟", f"n:{chat_id}:word_mute_minutes:+5:wordpenalty")],
                [self._button("⬅️ 返回违禁词", f"v:{chat_id}:words")],
            ])
        elif what == "wordnotice":
            text = (
                "🕒 违禁词删除提醒\n\n"
                f"当前：{'关闭（不在群内通知）' if conf['word_notice_seconds'] == 0 else str(conf['word_notice_seconds']) + ' 秒后自动撤回'}\n"
                "仅当确实成功删除违禁词消息后，才在群内发送简短提醒。\n"
                "默认关闭、静默处理；开启后将尽量在设定时间后撤回提醒。"
            )
            keyboard.extend([
                [self._button("🔕 关闭", f"wn:{chat_id}:0"),
                 self._button("10 秒", f"wn:{chat_id}:10")],
                [self._button("30 秒", f"wn:{chat_id}:30"),
                 self._button("60 秒", f"wn:{chat_id}:60")],
                [self._button("⬅️ 返回违禁词", f"v:{chat_id}:words")],
            ])
        elif what == "domains":
            domains = [r["value"] for r in self.store.filters(chat_id, "domain")]
            text = "🌐 允许的域名：\n" + ("\n".join(domains[:60]) if domains else "暂无")
            text += "\n\n在群内用 /allowdomain example.com 添加。"
        elif what == "logs":
            text = (
                f"🧾 独立管理日志群：{conf['log_chat_id'] or '尚未设置'}\n"
                "日志群必须和当前群不同，并且你和机器人都需要具备日志目标群的管理权限。\n"
                "请在日志群发送 /id 获取群 ID，再回当前群：\n"
                "/setlog -1001234567890\n"
                "关闭日志：/setlog off\n"
                "自动删除和禁言默认不在当前群公开播报。"
            )
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
        text, keyboard = self.admin_ui.decorate(chat_id, what, text, keyboard)
        keyboard.append([self._button("⬅️ 返回设置", f"g:{chat_id}")])
        return self._edit_or_send(
            user_id, message_id, text, keyboard)

    def _private_schedule(self, user_id, chat_id, task_id, action, message_id, query_id):
        item = self.store.schedule(chat_id, task_id)
        if not item:
            self.answer(query_id, "任务不存在或已经删除。", alert=True)
            return self._private_view(
                user_id, chat_id, "schedules", message_id, verified_admin=True)
        if action == "sx":
            self.store.delete_schedule(chat_id, task_id)
            self.log(chat_id, f"删除定时公告 #{task_id}", actor=user_id)
            self.answer(query_id, "任务已删除")
            return self._private_view(
                user_id, chat_id, "schedules", message_id, verified_admin=True)
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
            "/addwords 词1,词2 /delwords 词1,词2\n"
            "/spam on|off /mediaflood on|off /automute on|off\n"
            "/nameguard on|off /guardset name_length_limit 40\n"
            "/wordloose on|off /wordexcept 词 | 完整文本\n"
            "/wordtest 文本 /guardset flood_limit 7\n"
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

    def _private_word_input(self, uid, chat_id, operation, text):
        """One-shot text editor, scoped to the verified group/admin identity."""
        if len(text) > 3500:
            raise ValueError("输入太长，每次最多 3500 字")
        if operation in ("add", "del"):
            labels = [part.strip() for part in re.split(
                r"[,，、;\n]+", text) if part.strip()]
            if not 1 <= len(labels) <= 80:
                raise ValueError("每次请输入 1–80 条，以逗号或换行分隔")
            pairs = []
            for label in labels:
                key = normalize(label)
                if not 2 <= len(key) <= 80:
                    raise ValueError("每条违禁词应为 2–80 字，未保存任何内容")
                pairs.append((key, label))
            if operation == "add":
                count = self.store.add_filters_bulk(chat_id, "word", pairs)
            else:
                count = sum(self.store.del_filter(chat_id, "word", key)
                            for key in {k for k, _label in pairs})
            self.log(chat_id, f"私聊批量{'添加' if operation == 'add' else '删除'} "
                     f"{count} 条违禁词", actor=uid)
            return f"✅ 已{'导入' if operation == 'add' else '删除'} {count} 条违禁词。"
        if operation in ("except", "unexcept"):
            phrase, sep, context = text.partition("|")
            keyword, sentence = normalize(phrase.strip()), normalize(context.strip())
            if not sep or not 2 <= len(keyword) <= 80 or not 3 <= len(sentence) <= 200:
                raise ValueError("请输入：违禁词 | 完整允许文本（例如：广告 | 学校广告设计）")
            if operation == "except" and keyword not in sentence:
                raise ValueError("允许文本必须包含要豁免的违禁词")
            key = f"{keyword}|{sentence}"
            if operation == "except":
                self.store.add_filter(chat_id, "word_exception", key, key)
            else:
                self.store.del_filter(chat_id, "word_exception", key)
            self.log(chat_id, "通过私聊修改关键词精确例外", actor=uid)
            return "✅ 已更新精确例外（只对完整相同的消息豁免对应关键词）。"
        if operation == "test":
            found = match_words(
                text, [row["key"] for row in self.store.filters(chat_id, "word")],
                loose=self.store.settings(chat_id)["word_loose_match"],
                exceptions=[row["key"] for row in
                            self.store.filters(chat_id, "word_exception")])
            return ("🧪 测试结果：" + (
                f"命中 {str(found)[:70]}" if found else "没有命中违禁词") +
                "\n此操作不会处罚任何成员。")
        raise ValueError("未知的违禁词编辑操作")

    def _private_message(self, message):
        sender = message.get("from", {})
        uid = sender.get("id")
        text = (message.get("text") or "").strip()
        if not uid:
            return
        cmd = text.split(maxsplit=1)[0].split("@")[0].lower() if text else ""
        if cmd in ("/start", "/groups", "/panel", "/help", "/commands"):
            self.admin_ui.cancel(uid)
        elif self.admin_ui.input(message):
            return
        if cmd == "/cancel" and uid in self._word_wizards:
            chat_id, _operation, _expires = self._word_wizards.pop(uid)
            return self.send(uid, "✅ 已取消本次编辑。",
                             reply_markup=self._word_navigation(chat_id))
        if cmd in ("/start", "/groups", "/panel"):
            self._word_wizards.pop(uid, None)
            payload = text.partition(" ")[2]
            if cmd == "/start" and re.fullmatch(r"member_\d{1,18}", payload):
                return self.member_ui.render(-int(payload[7:]), uid)
            self._private_groups(uid)
        elif cmd == "/id":
            self.send(uid, f"🆔 你的 Telegram 数字ID：{uid}")
        elif cmd in ("/help", "/commands"):
            self._word_wizards.pop(uid, None)
            self.send(uid, self.help_text())
        elif cmd == "/health" and uid in self.owner_ids:
            self.send(uid, "✅ 数据库连接正常，机器人运行中。")
        elif uid in self._word_wizards and not message.get("sender_chat"):
            chat_id, operation, expiry = self._word_wizards.pop(uid)
            keyboard = self._word_navigation(chat_id)
            if time.monotonic() >= expiry:
                return self.send(uid, "⏰ 编辑已过期，请重新进入群管理面板。",
                                 reply_markup=keyboard)
            if not self.store.chat(chat_id) or not self.is_admin(chat_id, uid):
                return self.send(uid, "⚠️ 当前已无该群管理权限，未修改任何规则。",
                                 reply_markup=keyboard)
            try:
                result = self._private_word_input(uid, chat_id, operation, text)
            except ValueError as exc:
                self.send(uid, f"⚠️ {str(exc)[:180]}\n如需重试，请重新点击编辑按钮。",
                          reply_markup=keyboard)
                return
            self.send(uid, result, reply_markup=keyboard)
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
        if data.startswith("pc:"):
            return self.member_ui.callback(query)
        # Management callbacks work only in the requesting user's private chat.
        if chat.get("type") != "private" or chat.get("id") != user_id:
            self.answer(qid, "请私聊机器人操作。", alert=True)
            return
        if data == "groups":
            self._word_wizards.pop(user_id, None)
            self.answer(qid)
            return self._private_groups(user_id, message.get("message_id"))
        parts = data.split(":")
        if len(parts) not in (2, 3, 4, 5) or parts[0] not in (
            "g", "t", "v", "n", "w", "wl", "ws", "wp", "wn",
            "fs", "fp", "fn",
            "s", "sd", "sp", "sx", "ad", "ac"):
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
        if parts[0] in ("ad", "ac"):
            self._word_wizards.pop(user_id, None)
            return self.admin_ui.callback(query, chat_id, parts)
        self.admin_ui.cancel(user_id)
        if parts[0] != "w":
            self._word_wizards.pop(user_id, None)
        if parts[0] in ("fs", "fp", "fn"):
            if len(parts) != 3:
                return self.answer(qid, "防刷屏按钮参数无效", alert=True)
            operation, setting = parts[0], parts[2]
            conf = self.store.settings(chat_id)
            if operation == "fs":
                if setting not in ("on", "off"):
                    return self.answer(qid, "防刷屏状态参数无效", alert=True)
                on = setting == "on"
                if conf["anti_flood"] == on:
                    return self.answer(qid, "当前已经是此状态")
                if (on and not conf["anti_flood"]
                        and not self.bot_can(chat_id, "can_delete_messages")):
                    return self.answer(qid, "机器人缺少删除消息的权限", alert=True)
                self.store.set(chat_id, "anti_flood", on)
                description = f"防刷屏：{'启用' if on else '关闭'}"
                page = "flood"
            elif operation == "fp":
                if setting not in ("delete", "mute"):
                    return self.answer(qid, "无效的防刷屏处罚方式", alert=True)
                if conf["flood_penalty"] == setting:
                    return self.answer(qid, "当前已经是此处罚方式")
                if setting == "mute":
                    if not self.is_supergroup(chat_id):
                        return self.answer(qid, "禁言仅支持超级群组", alert=True)
                    if (not self.bot_can(chat_id, "can_delete_messages")
                            or not self.bot_can(chat_id, "can_restrict_members")):
                        return self.answer(
                            qid, "需要删除消息与限制成员两项权限", alert=True)
                self.store.set(chat_id, "flood_penalty", setting)
                description = ("防刷屏处罚：仅删除" if setting == "delete"
                               else "防刷屏处罚：删除并临时禁言")
                page = "floodpenalty"
            else:
                try:
                    seconds = int(setting)
                except ValueError:
                    return self.answer(qid, "无效的提醒时间", alert=True)
                if seconds not in (0, 10, 30, 60):
                    return self.answer(qid, "提醒时间只能选关闭/10/30/60秒", alert=True)
                if conf["flood_notice_seconds"] == seconds:
                    return self.answer(qid, "当前已经是此提醒设置")
                if seconds and not self.bot_can(chat_id, "can_delete_messages"):
                    return self.answer(qid, "机器人缺少删除消息权限", alert=True)
                self.store.set(chat_id, "flood_notice_seconds", seconds)
                description = ("防刷屏提醒：关闭" if seconds == 0
                               else f"防刷屏提醒：约 {seconds} 秒")
                page = "floodnotice"
            self.answer(qid, "✅ " + description)
            updated = self._private_view(
                user_id, chat_id, page, message.get("message_id"),
                verified_admin=True)
            # Deliver the requested page before sending the optional
            # audit message to a second Telegram chat (another API RTT).
            self.log(chat_id, description, actor=user_id)
            return updated
        if parts[0] in ("wl", "ws", "wp", "wn"):
            if len(parts) != 3:
                return self.answer(qid, "违禁词按钮参数无效", alert=True)
            operation, setting = parts[0], parts[2]
            if operation == "wl":
                try:
                    page = int(setting)
                except ValueError:
                    return self.answer(qid, "页码无效", alert=True)
                if not 0 <= page <= 100:
                    return self.answer(qid, "页码超出范围", alert=True)
                self.answer(qid)
                return self._private_word_list(
                    user_id, chat_id, page, message.get("message_id"),
                    verified_admin=True)
            conf = self.store.settings(chat_id)
            if operation == "ws":
                if setting not in ("on", "off"):
                    return self.answer(qid, "开关参数无效", alert=True)
                new_value = setting == "on"
                if (new_value and not conf["word_filter_enabled"]
                        and not self.bot_can(chat_id, "can_delete_messages")):
                    return self.answer(qid, "机器人缺少删除消息权限", alert=True)
                self.store.set(chat_id, "word_filter_enabled", new_value)
                label = f"违禁词状态：{'启用' if new_value else '关闭'}"
            elif operation == "wp":
                if setting not in ("delete", "mute"):
                    return self.answer(qid, "处罚选项无效", alert=True)
                if setting == "mute":
                    if not self.is_supergroup(chat_id):
                        return self.answer(qid, "禁言只支持超级群组", alert=True)
                    if (not self.bot_can(chat_id, "can_delete_messages")
                            or not self.bot_can(chat_id, "can_restrict_members")):
                        return self.answer(
                            qid, "机器人需要删除消息和限制成员权限", alert=True)
                self.store.set(chat_id, "word_penalty", setting)
                label = "处罚：仅删除" if setting == "delete" else "处罚：删除并禁言"
            else:
                try:
                    seconds = int(setting)
                except ValueError:
                    return self.answer(qid, "提醒时间格式无效", alert=True)
                if seconds not in (0, 10, 30, 60):
                    return self.answer(qid, "提醒时间只能选关闭/10/30/60秒", alert=True)
                if seconds and not self.bot_can(chat_id, "can_delete_messages"):
                    return self.answer(qid, "机器人缺少删除消息权限", alert=True)
                self.store.set(chat_id, "word_notice_seconds", seconds)
                label = "删除提醒：关闭" if not seconds else f"删除提醒：约 {seconds} 秒"
            self.answer(qid, "✅ " + label)
            page = "wordpenalty" if operation == "wp" else (
                "wordnotice" if operation == "wn" else "words")
            updated = self._private_view(
                user_id, chat_id, page, message.get("message_id"),
                verified_admin=True)
            self.log(chat_id, label, actor=user_id)
            return updated
        if parts[0] == "w":
            if len(parts) != 3 or parts[2] not in WORD_OPERATIONS:
                return self.answer(qid, "未知违禁词管理操作", alert=True)
            if len(self._word_wizards) > 2000:
                now = time.monotonic()
                for old_uid, (_cid, _act, until) in list(self._word_wizards.items()):
                    if until <= now:
                        del self._word_wizards[old_uid]
            self._word_wizards[user_id] = (chat_id, parts[2], time.monotonic() + 300)
            self.answer(qid, "请在私聊中输入内容")
            prompts = {
                "add": "➕ 发送违禁词，多个词用逗号或换行分隔，每次最多80条",
                "del": "➖ 发送要删除的词，多个词用逗号或换行分隔",
                "except": "🛡️ 添加精确例外：违禁词 | 完整允许文本",
                "unexcept": "❌ 删除精确例外：违禁词 | 完整允许文本",
                "test": "🧪 发送需要测试的文字（不会自动删除或处罚）",
            }
            self.send(user_id, f"{prompts[parts[2]]}\n"
                      "有效期5分钟；发送 /cancel 取消。"
                      "\n所有操作仅影响当前选择的群组。",
                      reply_markup=self._word_navigation(chat_id))
            return
        if parts[0] == "g" and len(parts) == 2:
            self.answer(qid)
            return self._private_panel(
                user_id, chat_id, message.get("message_id"),
                verified_admin=True)
        if parts[0] == "n":
            if len(parts) != 5:
                return self.answer(qid, "数字设置按钮参数无效", alert=True)
            key, raw_delta, origin = parts[2], parts[3], parts[4]
            controls = PAGE_NUMBERS.get(origin, ())
            steps = {name: step for name, _label, step in controls}
            if key not in NUMBER_LIMITS or key not in steps:
                return self.answer(qid, "没有此数字设置项", alert=True)
            try:
                delta = int(raw_delta)
            except ValueError:
                return self.answer(qid, "设置变化量无效", alert=True)
            if abs(delta) != steps[key]:
                return self.answer(qid, "不允许自定义按钮变化量", alert=True)
            previous = self.store.settings(chat_id)[key]
            low, high = NUMBER_LIMITS[key]
            proposed = previous + delta
            if not low <= proposed <= high:
                return self.answer(qid, f"范围必须在 {low}–{high} 之间", alert=True)
            self.store.set(chat_id, key, proposed)
            self.answer(qid, f"✅ {key} = {proposed}")
            updated = self._private_view(
                user_id, chat_id, origin, message.get("message_id"),
                verified_admin=True)
            self.log(chat_id, f"设置 {key}：{previous} → {proposed}", actor=user_id)
            return updated
        if parts[0] == "v" and len(parts) == 3:
            self.answer(qid)
            return self._private_view(
                user_id, chat_id, parts[2], message.get("message_id"),
                verified_admin=True)
        if parts[0] in ("s", "sd", "sp", "sx") and len(parts) == 3:
            try:
                task_id = int(parts[2])
            except ValueError:
                self.answer(qid, "任务编号无效", alert=True)
                return
            return self._private_schedule(user_id, chat_id, task_id,
                                          parts[0], message.get("message_id"), qid)
        action = parts[2] if len(parts) >= 3 else ""
        if action not in TOGGLES:
            self.answer(qid, "不允许修改这个设置。", alert=True)
            return
        if parts[0] != "t" or len(parts) not in (3, 4):
            return self.answer(qid, "按钮参数无效", alert=True)
        origin = parts[3] if len(parts) == 4 else None
        if origin is not None and action not in {
            key for _label, key in PAGE_SWITCHES.get(origin, ())
        }:
            return self.answer(qid, "非法功能页开关", alert=True)
        needed = ("can_restrict_members" if action in (
            "verify_enabled", "block_new_bots", "auto_mute_enabled",
            "name_guard_enabled")
                  else "can_delete_messages")
        if action not in ("verify_enabled", "block_new_bots", "auto_mute_enabled",
                          "name_guard_enabled",
                          "block_links", "block_forward", "anti_flood",
                          "repeat_photo_enabled", "spam_guard_enabled",
                          "word_loose_match", "media_flood_enabled",
                          "block_external_channels"):
            needed = None
        current = self.store.settings(chat_id)[action]
        if needed and not current and not self.bot_can(chat_id, needed):
            self.answer(qid, "机器人缺少必要管理员权限，请先授予权限。", alert=True)
            return
        if (action in ("verify_enabled", "block_new_bots", "auto_mute_enabled",
                       "name_guard_enabled") and not current
                and not self.is_supergroup(chat_id)):
            self.answer(qid, "该功能仅支持超级群组。", alert=True)
            return
        self.store.set(chat_id, action, not current)
        self.answer(qid, "设置已保存")
        if origin:
            self._private_view(
                user_id, chat_id, origin, message.get("message_id"),
                verified_admin=True)
        else:
            self._private_panel(
                user_id, chat_id, message.get("message_id"),
                verified_admin=True)
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
        attempt_limit = self.store.settings(chat_id)["verify_attempt_limit"]
        if pending["attempts"] >= attempt_limit:
            return self.answer(qid, "答错次数已用完，请联系管理员或等待超时后重新加入。", alert=True)
        if chosen != pending["answer"]:
            pending = self.store.verification_mistake(chat_id, user_id)
            left = max(0, attempt_limit - pending["attempts"])
            return self.answer(qid, f"答案不正确，剩余 {left} 次。", alert=True)
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
            if (settings["name_guard_enabled"] and not settings["verify_enabled"]
                    and self.is_supergroup(chat_id)):
                nickname = (str(member.get("first_name") or "") + " "
                            + str(member.get("last_name") or "")).strip()
                if len(nickname) > settings["name_length_limit"]:
                    # Unknown user status fails open; never restrict an admin
                    # promoted during a join, even if our cached list is stale.
                    try:
                        status = self.api.call(
                            "getChatMember", chat_id=chat_id, user_id=uid)
                    except TelegramAPIError:
                        status = None
                    if (status is not None and not admin_status(status)
                            and self.bot_can(chat_id, "can_restrict_members")):
                        restricted = self.api.safe(
                            "restrictChatMember", chat_id=chat_id, user_id=uid,
                            permissions=RESTRICTED,
                            until_date=int(time.time()) + 1800)
                        if restricted is True:
                            self.store.stat(chat_id, "actions")
                            self.log(chat_id, "新成员昵称过长，限制发言30分钟",
                                     target=uid)
                            continue
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

    def cleanup_word_notices(self):
        """Delete opt-in short-lived public notices, including after restart."""
        now = int(time.time())
        for notice in self.store.pending_notice_deletion_due(now=now):
            cid, mid = notice["chat_id"], notice["message_id"]
            # Do not retry a stale or irrevocably undeletable reminder forever.
            if notice["delete_at"] < now - 3600:
                self.store.clear_notice_deletion(cid, mid)
                continue
            if self.api.safe("deleteMessage", chat_id=cid, message_id=mid) is True:
                self.store.clear_notice_deletion(cid, mid)

    def _post_moderation_notice(self, chat_id, reason, seconds):
        """Best-effort expiring notice, throttled across keyword/flood policies."""
        if seconds not in (10, 30, 60) or reason not in ("违禁词", "防刷屏"):
            return
        tick = time.monotonic()
        if tick - self._last_moderation_notice.get(chat_id, -1e10) < 30:
            return
        notice = self.send(
            chat_id, f"⛔ {reason}违规消息已清理（本提醒约 {seconds} 秒后撤回）",
            disable_notification=True)
        if isinstance(notice, dict) and notice.get("message_id"):
            self._last_moderation_notice[chat_id] = tick
            self.store.schedule_notice_deletion(
                chat_id, notice["message_id"], int(time.time()) + seconds)
        if len(self._last_moderation_notice) > 5000:
            self._last_moderation_notice = {
                cid: last for cid, last in self._last_moderation_notice.items()
                if tick - last < 3600}

    def _moderate(self, message, *, is_edit=False):
        chat = message["chat"]
        chat_id = chat["id"]
        sender = message.get("from") or {}
        user_id = sender.get("id")
        sender_chat = message.get("sender_chat") or {}
        # Preserve genuine anonymous group admins and linked discussion posts.
        if sender_chat:
            if sender_chat.get("id") == chat_id or message.get("is_automatic_forward"):
                return
            settings = self.store.settings(chat_id)
            if not settings["block_external_channels"]:
                return
            target = sender_chat.get("id")
            if not target:
                return
            reason = "外部频道发言"
            if settings["observe_only"]:
                self.store.audit(chat_id, message["message_id"], target, reason, "observed", "channel")
                return True
            deleted = (self.bot_can(chat_id, "can_delete_messages") and self.api.safe(
                "deleteMessage", chat_id=chat_id, message_id=message["message_id"]) is True)
            self.store.audit(chat_id, message["message_id"], target, reason,
                             "deleted" if deleted else "failed", "channel")
            if deleted:
                self.store.stat(chat_id, "deleted")
                self.log(chat_id, "自动删除外部频道发言", target=target)
            return False
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
                          self.flood,
                          exceptions=[r["key"] for r in
                                      self.store.filters(chat_id, "word_exception")],
                          is_edit=is_edit)
        if not reason and not is_edit and settings["repeat_photo_enabled"]:
            reason = self.photo_guard.check(
                message, limit=settings["repeat_photo_limit"],
                window=settings["repeat_photo_window"])
        if reason:
            # Refresh membership only before deletion. An administrator promoted
            # during the short admin-list cache window must never be auto-deleted.
            try:
                fresh = self.api.call("getChatMember", chat_id=chat_id, user_id=user_id)
            except TelegramAPIError:
                # Missing membership evidence is not permission to delete.
                return False
            if admin_status(fresh):
                return True
            if settings["observe_only"]:
                self.store.audit(chat_id, message["message_id"], user_id, reason, "observed")
                return True
            if not self.bot_can(chat_id, "can_delete_messages"):
                self.store.audit(chat_id, message["message_id"], user_id, reason, "failed")
                return False
            deleted = self.api.safe("deleteMessage", chat_id=chat_id,
                                    message_id=message["message_id"])
            self.store.audit(chat_id, message["message_id"], user_id, reason,
                             "deleted" if deleted is True else "failed")
            if deleted is True:
                self.store.stat(chat_id, "deleted")
                self.log(chat_id, f"自动删除（{reason}）", target=user_id)
                flood_reason = reason in FLOOD_REASONS
                keyword_reason = reason == "违禁词"
                specific_minutes = (
                    settings["word_mute_minutes"]
                    if keyword_reason and settings["word_penalty"] == "mute"
                    else settings["flood_mute_minutes"]
                    if flood_reason and settings["flood_penalty"] == "mute"
                    else None)
                specific_muted = False
                if (specific_minutes is not None and not is_edit
                        and fresh.get("status") in ("member", "restricted")
                        and self.is_supergroup(chat_id)
                        and self.bot_can(chat_id, "can_restrict_members")
                        and self.can_target(chat_id, user_id)):
                    specific_muted = self.api.safe(
                        "restrictChatMember", chat_id=chat_id, user_id=user_id,
                        permissions=RESTRICTED,
                        until_date=int(time.time()) + specific_minutes * 60) is True
                    if specific_muted:
                        self.store.stat(chat_id, "actions")
                        self.log(chat_id,
                                 f"{'违禁词' if keyword_reason else '防刷屏'}违规，"
                                 f"禁言 {specific_minutes} 分钟",
                                 target=user_id)
                if keyword_reason:
                    self._post_moderation_notice(
                        chat_id, "违禁词", settings["word_notice_seconds"])
                elif flood_reason:
                    self._post_moderation_notice(
                        chat_id, "防刷屏", settings["flood_notice_seconds"])
                if (settings["auto_mute_enabled"] and not is_edit
                        and not specific_muted):
                    history = self.store.record_moderation(
                        chat_id, user_id, message["message_id"], reason)
                    threshold = settings["auto_mute_after"]
                    if (history["created"] and history["recent"] >= threshold
                            and history["recent"] % threshold == 0
                            and self.is_supergroup(chat_id)
                            and self.bot_can(chat_id, "can_restrict_members")
                            and self.can_target(chat_id, user_id)):
                        muted = self.api.safe(
                            "restrictChatMember", chat_id=chat_id,
                            user_id=user_id, permissions=RESTRICTED,
                            until_date=int(time.time())
                            + settings["auto_mute_minutes"] * 60)
                        if muted is True:
                            self.store.stat(chat_id, "actions")
                            self.log(chat_id,
                                     f"10分钟内违规 {history['recent']} 次，"
                                     f"自动禁言 {settings['auto_mute_minutes']} 分钟",
                                     target=user_id)
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
                previous = self._reply_time.get(key)
                if previous is not None and now - previous < 60:
                    continue
                group_recent = self._reply_group_time.setdefault(chat_id, deque())
                while group_recent and now - group_recent[0] >= 60:
                    group_recent.popleft()
                if len(group_recent) >= settings["reply_group_limit"]:
                    return True
                group_recent.append(now)
                if len(self._reply_group_time) > 1000:
                    self._reply_group_time = {cid: times for cid, times in self._reply_group_time.items()
                                              if times and now-times[-1] < 60}
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
        if settings["ocr_mode"] == "off" or not settings["word_filter_enabled"]:
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
            if mode == "off" or not settings["word_filter_enabled"]:
                continue
            matched = matched_words(
                text, confidence,
                [r["key"] for r in self.store.filters(cid, "word")],
                threshold=settings["ocr_confidence"],
                exceptions=[r["key"] for r in self.store.filters(cid, "word_exception")])
            if not matched:
                continue
            try:
                person = self.api.call("getChatMember", chat_id=cid, user_id=uid)
            except TelegramAPIError:
                continue
            if admin_status(person):
                continue
            self.store.stat(cid, "ocr_hits")
            if mode == "review" or settings["observe_only"]:
                self.store.audit(cid, event["message_id"], uid, "OCR违禁词", "observed", "ocr")
                # No raw image, OCR text, or untrusted URL is stored in logs.
                self.log(cid, f"🔎 OCR待审核 消息 {event['message_id']}："
                         f"命中 {','.join(matched)} / 可信度 {confidence:.0f}",
                         target=uid)
                continue
            if mode != "delete" or not self.bot_can(cid, "can_delete_messages"):
                continue
            deleted = self.api.safe(
                "deleteMessage", chat_id=cid, message_id=event["message_id"])
            self.store.audit(cid, event["message_id"], uid, "OCR违禁词",
                             "deleted" if deleted is True else "failed", "ocr")
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
        # Public member commands are messages too: inspect them before any
        # command can reply, create an order, or bypass the flood counters.
        allowed = self._moderate(message, is_edit="edited_message" in update)
        if allowed is False:
            return
        if "edited_message" not in update:
            from .commands import handle_command
            if handle_command(self, message):
                return
        if "edited_message" not in update and allowed is not False:
            self._maybe_queue_ocr(message)
        if "edited_message" not in update and allowed is not False:
            sender = message.get("from") or {}
            if (sender.get("id") and not sender.get("is_bot")
                and not message.get("sender_chat")
                and not (message.get("text") or "").startswith("/")
                and (message.get("text") or message.get("caption"))):
                self.store.record_activity(chat_id, sender["id"])
