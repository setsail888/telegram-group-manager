"""Telegram event routing, authorization, private settings and join challenge."""
from .i18n import error_text, msg as _ui, join_text, LocalizedAPI, CURRENT_LANGUAGE, normalize_locale
from .language_ui import LanguageUI
import json
import logging
import math
import os
import random
import re
import secrets
import time
from collections import deque

from .api import TelegramAPIError
from .economy import Economy
from .raffle import Raffles
from .moderation import FloodGuard, PhotoRepeatGuard, evaluate, normalize, message_word_hit
from .ocr import OCRWorker, MAX_OCR_FILE_BYTES, matched_words
from .scheduler import format_run, next_occurrence
from .store import TOGGLES, NUMBER_LIMITS
from .admin_ui import AdminUI
from .member_ui import MemberUI
from .raid import JoinGuard
from .word_ui import WordUI
from .word_library import describe_hit
from .variants import context_matches
from .membership import is_current_member
from .cross_flood import CrossAccountGuard, REASON as CROSS_REASON
from .flood_penalty import penalty_label, apply_penalty
from .flood_ui import FloodUI
from .feature_help import FeatureHelp, ADMIN_PAGES

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
    (_ui('🎉 免费抽奖'), "raffle"), (_ui('🔗 链接管理'), "links"),
    (_ui('🛡️ 智能反垃圾'), "spam"), (_ui('🚦 防刷屏'), "flood"),
    (_ui('⛔ 违禁词'), "words"), (_ui('🧩 新人验证'), "verify"),
    (_ui('👤 新成员限制'), "members"), (_ui('👋 欢迎消息'), "welcome"),
    (_ui('🤖 机器人防护'), "bots"), (_ui('🔁 禁止转发'), "forward"),
    (_ui('🖼️ 图片防刷'), "photo"), (_ui('🔎 图片 OCR'), "ocr"),
    (_ui('🌐 域名白名单'), "domains"), (_ui('💬 自动回复'), "replies"),
    (_ui('📅 定时公告'), "schedules"), (_ui('🪙 积分签到'), "points"),
    (_ui('🎁 积分商城'), "shop"), (_ui('📊 今日统计'), "stats"),
    (_ui('📈 最近7天'), "stats7"), (_ui('🏆 活跃排行'), "activity"),
    (_ui('⚙️ 规则总览'), "overview"), (_ui('📖 命令指南'), "help"),
    (_ui('🔍 防护记录'), "audit"), (_ui('👮 成员管理'), "manage"),
    (_ui('🚨 集中入群防护'), "raid"), (_ui('🔐 权限检查'), "permissions"),
    (_ui('🧾 管理日志'), "logs"), (_ui('🔄 切换群组'), "groups"),
    (_ui('🌐 群内提示语言'), "language"),
)
PAGE_SWITCHES = {
    "spam": ((_ui('🛡️ 智能反垃圾'), "spam_guard_enabled"),
             (_ui('🔗 链接拦截'), "block_links"),
             (_ui('🔁 禁止转发'), "block_forward"),
             (_ui('🔎 拆字识别'), "word_loose_match"),
             (_ui('⚖️ 自动禁言'), "auto_mute_enabled"),
             (_ui('🔍 仅观察内容过滤'), "observe_only"),
             (_ui('📣 拦截外部频道发言'), "block_external_channels")),
    "flood": ((_ui('🚦 防刷屏'), "anti_flood"),
              (_ui('🖼️ 媒体连发限制'), "media_flood_enabled"),
              (_ui('📚 相册按一条计数'), "album_single_enabled")),
    "floodmedia": ((_ui('🖼️ 媒体连发限制'), "media_flood_enabled"),
                   (_ui('📚 相册按一条计数'), "album_single_enabled")),
    "links": ((_ui('🔗 链接拦截'), "block_links"),),
    "forward": ((_ui('🔁 禁止转发'), "block_forward"),),
    "words": ((_ui('🔎 拆字违禁词'), "word_loose_match"),),
    "verify": ((_ui('🧩 新人验证'), "verify_enabled"),),
    "welcome": ((_ui('👋 欢迎语'), "welcome_enabled"),),
    "bots": ((_ui('🤖 拦截新人机器人'), "block_new_bots"),),
    "members": ((_ui('👤 超长昵称限制'), "name_guard_enabled"),),
    "photo": ((_ui('🖼️ 重复图片拦截'), "repeat_photo_enabled"),),
    "points": ((_ui('🪙 签到积分'), "points_enabled"),),
    "raid": ((_ui('🚨 自动检测集中入群'), "raid_enabled"),),
}
PAGE_NUMBERS = {
    "spam": (("mention_limit", _ui('批量提及次数'), 1),
             ("auto_mute_after", _ui('连续违规次数'), 1),
             ("auto_mute_minutes", _ui('自动禁言分钟'), 5)),
    "flood": (("flood_limit", _ui('短时消息条数'), 1),
              ("flood_window", _ui('刷屏窗口（秒）'), 5),
              ("repeat_limit", _ui('重复消息次数'), 1),
              ("repeat_window", _ui('重复判断（秒）'), 5),
              ("media_flood_limit", _ui('媒体连发条数'), 1),
              ("media_flood_window", _ui('媒体窗口（秒）'), 5)),
    "floodcount": (("flood_limit", _ui('触发条数'), 1),),
    "floodinterval": (("flood_window", _ui('检测间隔（秒）'), 5),),
    "floodrepeat": (("repeat_limit", _ui('重复消息次数'), 1),
                    ("repeat_window", _ui('重复判断（秒）'), 5)),
    "floodmedia": (("media_flood_limit", _ui('媒体连发条数'), 1),
                   ("media_flood_window", _ui('媒体窗口（秒）'), 5)),
    "floodpenalty": (("flood_mute_minutes", _ui('刷屏禁言分钟'), 5),
                     ("warn_limit", _ui('警告上限（与手动共用）'), 1)),
    "crossflood": (("cross_flood_limit", _ui('同内容总条数'), 1),
                   ("cross_flood_users", _ui('不同账号数'), 1),
                   ("cross_flood_window", _ui('检测窗口（秒）'), 5),
                   ("cross_flood_min_chars", _ui('文字最小长度'), 1)),
    "members": (("name_length_limit", _ui('允许昵称最大长度'), 5),),
    "wordpenalty": (("word_mute_minutes", _ui('违禁词禁言分钟'), 5),),
    "verify": (("verify_timeout", _ui('验证时限（秒）'), 60),
               ("verify_attempt_limit", _ui('答错上限'), 1)),
    "photo": (("repeat_photo_limit", _ui('重复图片次数'), 1),
              ("repeat_photo_window", _ui('图片窗口（秒）'), 30)),
    "points": (("checkin_award", _ui('签到基础奖励'), 1),),
    "ocr": (("ocr_confidence", _ui('最低可信度'), 5),),
    "replies": (("reply_group_limit", _ui('全群每分钟回复上限'), 1),),
    "manage": (("warn_limit", _ui('警告上限'), 1),),
    "raid": (("raid_join_limit", _ui('触发入群人数'), 1),
             ("raid_window", _ui('检测窗口（秒）'), 10),
             ("raid_duration", _ui('保护时长（分钟）'), 1)),
    "welcome": (("welcome_limit", _ui('每分钟欢迎上限'), 1),),
}
WORD_OPERATIONS = {"add", "del", "except", "unexcept", "test"}
FLOOD_REASONS = frozenset((_ui('短时刷屏'), _ui('重复刷屏'), _ui('媒体刷屏'), CROSS_REASON))


def admin_status(member):
    return isinstance(member, dict) and member.get("status") in ADMIN_STATUSES


class GroupBot:
    def __init__(self, api, store, bot_id, username, owner_ids=frozenset()):
        self.api = LocalizedAPI(api, store)
        self.store = store
        self.bot_id = int(bot_id)
        self.username = username
        self.owner_ids = owner_ids
        self.flood = FloodGuard()
        self.cross_guard = CrossAccountGuard()
        self.flood_ui = FloodUI(self)
        self.photo_guard = PhotoRepeatGuard()
        self.join_guard = JoinGuard()
        self.word_ui = WordUI(self)
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
        self.language_ui = LanguageUI(self)
        self.feature_help = FeatureHelp(self)
        self._reply_group_time = {}
        # Rate-limit ALL public moderation reminders per group across modules.
        self._last_moderation_notice = {}

    def send(self, chat_id, text, **kwargs):
        return self.api.safe("sendMessage", chat_id=chat_id, text=text[:4000], **kwargs)

    def answer(self, query_id, text="", alert=False):
        params = {"callback_query_id": query_id,
                  "text": text[:190], "show_alert": alert}
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
        summary = _ui('🛡️ 群组 {p0} | {p1}', p0=chat_id, p1=action[:120])
        if actor:
            summary += _ui(' | 操作者 {p0}', p0=actor)
        if target:
            summary += _ui(' | 用户 {p0}', p0=target)
        self.send(destination, summary)

    def _get_admins(self, chat_id):
        now = time.monotonic()
        cached = self._admins.get(chat_id)
        if cached and cached[0] > now:
            return cached[1]
        try:
            admins = self.api.call("getChatAdministrators", chat_id=chat_id)
        except TelegramAPIError as exc:
            LOG.warning(_ui('无法读取管理员名单（跳过本次自动过滤）: %s'), exc)
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
        return isinstance(member, dict) and (member.get("status") == "creator" or (
            member.get("status") == "administrator" and member.get(permission, False)))

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
        return isinstance(status, dict) and status.get("status") in ("member", "restricted", "left", "kicked")

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
                keyboard.append([self._button(chat["title"][:35], _ui('g:{p0}', p0=chat['chat_id']))])
            if len(keyboard) >= 12:
                break
        if not keyboard:
            return self._edit_or_send(
                user_id, message_id, _ui('暂未发现你有权管理的群组。\n请先将机器人加入群、授予管理权限，并在群里发送 /panel。'),
                [[self._button(_ui("🌐 界面语言 / Language"), "lang:self")]])
        keyboard.append([self._button(_ui("🌐 界面语言 / Language"), "lang:self")])
        return self._edit_or_send(
            user_id, message_id, _ui('📋 选择要管理的群组：'), keyboard)

    def _private_panel(self, user_id, chat_id, message_id=None, *,
                       verified_admin=False):
        info = self.store.chat(chat_id)
        # A callback has already performed a *fresh* getChatMember call.
        # A second identical network request on the same click adds latency
        # without meaningfully improving authorization.
        if not info or (not verified_admin and not self.is_admin(chat_id, user_id)):
            return self._edit_or_send(user_id, message_id, _ui('没有权限管理这个群组。'))
        settings = self.store.settings(chat_id)
        keyboard = []
        for index in range(0, len(MENU_MODULES), 2):
            row = []
            for label, page in MENU_MODULES[index:index + 2]:
                cb = "groups" if page == "groups" else _ui('v:{p0}:{p1}', p0=chat_id, p1=page)
                row.append(self._button(label, cb))
            keyboard.append(row)
        active = sum(bool(settings.get(key)) for key in
                     ("block_links", "anti_flood", "block_forward",
                      "spam_guard_enabled", "media_flood_enabled",
                      "repeat_photo_enabled", "verify_enabled",
                      "name_guard_enabled"))
        return self._edit_or_send(
            user_id, message_id,
            _ui('⚙️ 设置「{p0}」\n🛡️ 防护已开启 {p1}/8 项 · 群组 {p2}\n请选择要更改的项目（每个群独立设置）。', p0=info['title'], p1=active, p2=chat_id),
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
                    _ui('{p0} {p1}', p0=state, p1=label), _ui('t:{p0}:{p1}:{p2}', p0=chat_id, p1=key, p2=page)))
            keyboard.append(row)
        for name, label, step in PAGE_NUMBERS.get(page, ()):
            keyboard.append([
                self._button(_ui('➖ {p0}', p0=label), _ui('n:{p0}:{p1}:-{p2}:{p3}', p0=chat_id, p1=name, p2=step, p3=page)),
                self._button(_ui('{p0} ➕', p0=conf[name]), _ui('n:{p0}:{p1}:+{p2}:{p3}', p0=chat_id, p1=name, p2=step, p3=page)),
            ])
        return keyboard

    def _word_navigation(self, chat_id):
        """Always give users an exit from one-shot private keyword editing."""
        return {"inline_keyboard": [[
            self._button(_ui('⬅️ 返回违禁词管理'), _ui('v:{p0}:words', p0=chat_id)),
            self._button(_ui('⚙️ 返回群组设置'), _ui('g:{p0}', p0=chat_id)),
        ]]}

    def _private_word_list(self, user_id, chat_id, page=0, message_id=None, *,
                           verified_admin=False):
        """Separate, bounded list; the main keyword page never shows entries."""
        if not verified_admin and not self.is_admin(chat_id, user_id):
            return self._edit_or_send(user_id, message_id, _ui('没有权限。'))
        count = self.store.filter_count(chat_id, "word")
        last_page = max(0, (count - 1) // 8)
        page = min(last_page, page)
        rows = self.store.filter_page(chat_id, "word", page, page_size=8)
        listing = join_text('\n', (_ui('{p0}. {p1}', p0=page * 8 + i, p1=str(row['value'])[:80]) for i, row in enumerate(rows, 1))) or _ui('暂无违禁词')
        keyboard = self.admin_ui.word_buttons(user_id, chat_id, rows)
        navigation = []
        if page > 0:
            navigation.append(self._button(_ui('◀️ 上一页'), _ui('wl:{p0}:{p1}', p0=chat_id, p1=page - 1)))
        if page < last_page:
            navigation.append(self._button(_ui('下一页 ▶️'), _ui('wl:{p0}:{p1}', p0=chat_id, p1=page + 1)))
        if navigation:
            keyboard.append(navigation)
        self.feature_help.add_admin(keyboard, chat_id, "wordlist")
        keyboard.append([self._button(_ui('⬅️ 返回违禁词管理'), _ui('v:{p0}:words', p0=chat_id))])
        return self._edit_or_send(
            user_id, message_id,
            _ui('📋 违禁词列表 · 共 {p0} 条\n第 {p1}/{p2} 页\n\n{p3}', p0=count, p1=page + 1, p2=last_page + 1, p3=listing),
            keyboard)

    def _private_view(self, user_id, chat_id, what, message_id, *,
                      verified_admin=False):
        if not verified_admin and not self.is_admin(chat_id, user_id):
            return self._edit_or_send(user_id, message_id, _ui('没有权限。'))
        if what == "language":
            return self.language_ui.page(user_id, cid=chat_id, mid=message_id)
        keyboard = []
        conf = self.store.settings(chat_id)
        # The keyword page has its own compact button layout, matching the
        # reference bot, rather than an expanded rule list.
        if what not in ("words", "flood"):
            keyboard.extend(self._settings_controls(chat_id, what, conf))
        if what == "spam":
            text = (
                _ui('🛡️ 智能反垃圾\n链接：{p0} / 伪装链接与批量@：{p1}\n批量提及：超过 {p2} 个触发\n拆字关键词：{p3}\n违规自动禁言：{p4}\n累计 {p5} 次实际删除 / 10 分钟 → 禁言 {p6} 分钟\n检测文字及媒体说明中的显式@和隐藏提及；伪装链接同样遵守域名白名单。\n采用规则识别，可能漏判或误判，请先在测试群调整。\n注：自动禁言默认关闭，启用前请确认机器人限制成员权限。', p0=_ui('开') if conf['block_links'] else _ui('关'), p1=_ui('开') if conf['spam_guard_enabled'] else _ui('关'), p2=conf['mention_limit'], p3=_ui('开') if conf['word_loose_match'] else _ui('关'), p4=_ui('开') if conf['auto_mute_enabled'] else _ui('关'), p5=conf['auto_mute_after'], p6=conf['auto_mute_minutes'])
            )
        elif what == "flood":
            text = (
                _ui('💬 反刷屏\n\n状态：{p0}\n当前：{p1} 秒 / 超过 {p2} 条\n重复：{p3} 秒 / {p4} 次\n媒体连发：{p5}（{p6} 秒 / 超过 {p7} 条）\n跨账号重复：{p8}\n惩罚：{p9}\n删除提醒：{p10}', p0=_ui('✅ 启用') if conf['anti_flood'] else _ui('⭕ 关闭'), p1=conf['flood_window'], p2=conf['flood_limit'], p3=conf['repeat_window'], p4=conf['repeat_limit'], p5=_ui('✅ 开启') if conf['media_flood_enabled'] else _ui('关闭'), p6=conf['media_flood_window'], p7=conf['media_flood_limit'], p8=_ui('关闭') if conf['cross_flood_mode'] == 'off' else _ui('仅观察') if conf['cross_flood_mode'] == 'observe' else _ui('自动清理'), p9=penalty_label(conf), p10=str(conf['flood_notice_seconds']) + _ui(' 秒后撤回') if conf['flood_notice_seconds'] else _ui('关闭（静默）'))
            )
            if conf["auto_mute_enabled"] and conf["flood_penalty"] == "delete":
                text += _ui('\n注：全局「累计违规禁言」已启用，累计达到阈值仍可能被禁言。')
            keyboard.extend([
                [self._button(_ui('✅ 启用'), _ui('fs:{p0}:on', p0=chat_id)),
                 self._button(_ui('⭕ 关闭'), _ui('fs:{p0}:off', p0=chat_id))],
                [self._button(_ui('💭 触发条数'), _ui('v:{p0}:floodcount', p0=chat_id)),
                 self._button(_ui('⏱️ 检测间隔'), _ui('v:{p0}:floodinterval', p0=chat_id))],
                [self._button(_ui('🔁 重复判断'), _ui('v:{p0}:floodrepeat', p0=chat_id)),
                 self._button(_ui('🖼️ 媒体连发'), _ui('v:{p0}:floodmedia', p0=chat_id))],
                [self._button(_ui('⚖️ 惩罚'), _ui('v:{p0}:floodpenalty', p0=chat_id)),
                 self._button(_ui('🕒 删除提醒'), _ui('v:{p0}:floodnotice', p0=chat_id))],
                [self._button(_ui('👥 跨账号重复'), _ui('v:{p0}:crossflood', p0=chat_id))],
            ])
        elif what in ("floodcount", "floodinterval", "floodrepeat", "floodmedia"):
            labels = {
                "floodcount": _ui('💭 触发条数'),
                "floodinterval": _ui('⏱️ 检测间隔'),
                "floodrepeat": _ui('🔁 重复消息判断'),
                "floodmedia": _ui('🖼️ 媒体连发限制'),
            }
            if what == "floodmedia":
                seconds = conf["media_flood_window"]
                limit = conf["media_flood_limit"]
                enabled = conf["media_flood_enabled"] and conf["anti_flood"]
                penalty = _ui('删除触发消息') if conf["flood_penalty"] == "delete" else penalty_label(conf)
                notification = (
                    _ui('静默清理，不在群内发提示')
                    if conf["flood_notice_seconds"] == 0
                    else _ui('发送短提示，约 {p0} 秒后撤回', p0=conf['flood_notice_seconds'])
                )
                text = (
                    _ui('🖼️ 媒体连发限制\n\n状态：{p0}（本项{p1}，主防刷屏{p2}）\n规则：同一普通成员在 {p3} 秒内发送超过 {p4} 条媒体，从第 {p5} 条起触发清理。\n\n📌 计入：照片、视频、动图、贴纸、文件、语音、音频、视频留言；不同类型合计，内容不必相同。\n📌 不计入媒体次数：纯文字及编辑消息；管理员豁免。中间穿插文字也不会清空该时间窗口内的媒体计数。\n\n例如：{p6} 秒内已发 {p7} 条照片/视频，再发第 {p8} 条贴纸时会删除该贴纸，之前的媒体不会追溯删除。\n\n处罚：{p9}（在「防刷屏 → 惩罚」修改）。\n提醒：{p10}。\n生效条件：主防刷屏和本项两个开关都要开启，机器人还需有删除消息权限。纯文字仍可能触发普通刷屏规则。\n点击下方 ➖ / ➕ 可调整条数和时间。', p0=_ui('✅ 生效中') if enabled else _ui('⭕ 未生效'), p1=_ui('开') if conf['media_flood_enabled'] else _ui('关'), p2=_ui('开') if conf['anti_flood'] else _ui('关'), p3=seconds, p4=limit, p5=limit + 1, p6=seconds, p7=limit, p8=limit + 1, p9=penalty, p10=notification)
                )
                if conf["auto_mute_enabled"] and conf["flood_penalty"] == "delete":
                    text += _ui('\n⚠️ 已开启全局累计违规禁言，累计达到阈值仍可能被禁言。')
            else:
                current = (
                    _ui('短时消息：{p0} 秒内超过 {p1} 条', p0=conf['flood_window'], p1=conf['flood_limit'])
                    if what in ("floodcount", "floodinterval")
                    else _ui('重复消息：{p0} 秒内达到 {p1} 次', p0=conf['repeat_window'], p1=conf['repeat_limit'])
                )
                text = (_ui('{p0}\n\n{p1}\n\n点击下方 ➖ / ➕ 调整，仅对当前群生效。', p0=labels[what], p1=current))
            keyboard.append([self._button(_ui('⬅️ 返回防刷屏'), _ui('v:{p0}:flood', p0=chat_id))])
        elif what == "floodpenalty":
            text, controls = self.flood_ui.penalties(chat_id)
            keyboard.extend(controls)
        elif what == "crossflood":
            text, controls = self.flood_ui.cross(chat_id)
            keyboard.extend(controls)
        elif what in ("floodmutetime", "floodbantime"):
            text, controls = self.flood_ui.duration(chat_id, ban=what == "floodbantime")
            keyboard.extend(controls)
        elif what == "floodnotice":
            text = (
                _ui('🕒 反刷屏删除提醒\n\n当前：{p0}\n只有成功删除刷屏消息才发送一条简短提醒，不包含成员身份。\n默认关闭；每群最多约30秒一次，设定时间后尽量撤回。', p0=_ui('关闭（静默）') if conf['flood_notice_seconds'] == 0 else str(conf['flood_notice_seconds']) + _ui(' 秒后撤回'))
            )
            keyboard.extend([
                [self._button(_ui('🔕 关闭'), _ui('fn:{p0}:0', p0=chat_id)),
                 self._button(_ui('10 秒'), _ui('fn:{p0}:10', p0=chat_id))],
                [self._button(_ui('30 秒'), _ui('fn:{p0}:30', p0=chat_id)),
                 self._button(_ui('60 秒'), _ui('fn:{p0}:60', p0=chat_id))],
                [self._button(_ui('⬅️ 返回防刷屏'), _ui('v:{p0}:flood', p0=chat_id))],
            ])
        elif what == "links":
            text = (
                _ui('🔗 链接防护\n当前：{p0}\n识别普通网址、隐藏 text_link 和常见 Telegram 链接；打开智能反垃圾后还会识别部分 t[.]me 等变体。\n域名白名单：/allowdomain example.com\n移除：/deldomain example.com', p0=_ui('拦截外链') if conf['block_links'] else _ui('不拦截外链'))
            )
            keyboard.append([self._button(_ui('🌐 管理白名单'), _ui('v:{p0}:domains', p0=chat_id))])
        elif what == "forward":
            text = _ui('🔁 转发保护\n开启后自动删除普通成员转发的消息，管理员不受影响。')
        elif what == "verify":
            text = (
                _ui('🧩 新人验证\n当前：{p0}\n验证时限：{p1} 秒\n仅超级群组可用；新人通过点击数学按钮解除限制。', p0=_ui('开启') if conf['verify_enabled'] else _ui('关闭'), p1=conf['verify_timeout'])
            )
        elif what == "welcome":
            text = (
                _ui('👋 欢迎消息：{p0}\n当前欢迎语：{p1}\n管理员在群内：/setwelcome 欢迎 {{name}}！', p0=_ui('开启') if conf['welcome_enabled'] else _ui('关闭'), p1=self.store.content_text(chat_id, 'welcome_text')[:400])
            )
        elif what == "bots":
            text = (
                _ui('🤖 拦截新入群机器人：{p0}\n仅超级群组有效；开启后也可能拦截管理员邀请的其他机器人。', p0=_ui('开启') if conf['block_new_bots'] else _ui('关闭'))
            )
        elif what == "members":
            text = (
                _ui('👤 新成员昵称限制\n当前：{p0}\n昵称长度超过 {p1} 字时，将对普通新成员临时限制发言 30 分钟。\n默认关闭；管理员豁免，机器人需有「限制成员」权限。\n若同时开启新人验证，先执行新人验证，避免昵称限制提前解除验证禁言。\n本功能不永久封禁，适合先在测试群验证长昵称问题。', p0=_ui('开启') if conf['name_guard_enabled'] else _ui('关闭'), p1=conf['name_length_limit'])
            )
        elif what == "overview":
            text = (_ui('⚙️ 群规则总览\n') +
                    join_text('\n', (_ui('{p0}：{p1}', p0=label, p1=_ui('开') if conf[key] else _ui('关')) for label, key in ((_ui('🔗 链接'), 'block_links'), (_ui('🔁 转发'), 'block_forward'), (_ui('🚦 刷屏'), 'anti_flood'), (_ui('🛡️ 智能垃圾'), 'spam_guard_enabled'), (_ui('🖼️ 媒体连发'), 'media_flood_enabled'), (_ui('🔎 拆字关键词'), 'word_loose_match'), (_ui('⚖️ 自动禁言'), 'auto_mute_enabled'), (_ui('🧩 验证'), 'verify_enabled')))) +
                    _ui('\n\n设置均仅作用于当前群。'))
        elif what == "words":
            words_total = self.store.filter_count(chat_id, "word")
            exceptions_total = self.store.filter_count(chat_id, "word_exception")
            enabled = conf["word_filter_enabled"]
            punishment = (_ui('仅删除消息') if conf["word_penalty"] == "delete"
                          else _ui('删除并禁言 {p0} 分钟', p0=conf['word_mute_minutes']))
            reminder = (_ui('{p0} 秒后撤回', p0=conf['word_notice_seconds'])
                        if conf["word_notice_seconds"] else _ui('关闭（静默）'))
            text = (
                _ui('⛔ 违禁词\n\n状态：{p0}\n惩罚：{p1}\n删除提醒：{p2}\n规则：{p3} 条 · 精确例外：{p4} 条', p0=_ui('✅ 启用') if enabled else _ui('⭕ 关闭'), p1=punishment, p2=reminder, p3=words_total, p4=exceptions_total)
            )
            keyboard.extend([
                [self._button(_ui('✅ 启用'), _ui('ws:{p0}:on', p0=chat_id)),
                 self._button(_ui('⭕ 关闭'), _ui('ws:{p0}:off', p0=chat_id))],
                [self._button(_ui('➕ 添加违禁词'), _ui('w:{p0}:add', p0=chat_id)),
                 self._button(_ui('➖ 删除违禁词'), _ui('w:{p0}:del', p0=chat_id))],
                [self._button(_ui('📋 违禁词列表'), _ui('wl:{p0}:0', p0=chat_id))],
                [self._button(_ui('⚖️ 惩罚'), _ui('v:{p0}:wordpenalty', p0=chat_id)),
                 self._button(_ui('🕒 删除提醒'), _ui('v:{p0}:wordnotice', p0=chat_id))],
                [self._button(
                    _ui('{p0} 🔎 拆字违禁词', p0='✅' if conf['word_loose_match'] else '⭕'),
                    _ui('t:{p0}:word_loose_match:words', p0=chat_id))],
                [self._button(_ui('🛡️ 添加精确例外'), _ui('w:{p0}:except', p0=chat_id)),
                 self._button(_ui('❌ 删除精确例外'), _ui('w:{p0}:unexcept', p0=chat_id))],
                [self._button(_ui('🧪 测试关键词匹配'), _ui('w:{p0}:test', p0=chat_id))],
            ])
        elif what == "wordpenalty":
            text = (
                _ui('⚖️ 违禁词处罚\n\n当前：{p0}\n禁言时长：{p1} 分钟\n仅处理命中违禁词的普通群成员；默认仅删除，保持静默。\n启用禁言需要机器人具有「限制成员」权限，并且群是超级群。', p0=_ui('仅删除消息') if conf['word_penalty'] == 'delete' else _ui('删除消息并临时禁言'), p1=conf['word_mute_minutes'])
            )
            keyboard.extend([
                [self._button(_ui('✅ 仅删除'), _ui('wp:{p0}:delete', p0=chat_id)),
                 self._button(_ui('🔇 删除并禁言'), _ui('wp:{p0}:mute', p0=chat_id))],
                [self._button(_ui('➖ 5 分钟'), _ui('n:{p0}:word_mute_minutes:-5:wordpenalty', p0=chat_id)),
                 self._button(_ui('➕ 5 分钟'), _ui('n:{p0}:word_mute_minutes:+5:wordpenalty', p0=chat_id))],
                [self._button(_ui('⬅️ 返回违禁词'), _ui('v:{p0}:words', p0=chat_id))],
            ])
        elif what == "wordnotice":
            text = (
                _ui('🕒 违禁词删除提醒\n\n当前：{p0}\n仅当确实成功删除违禁词消息后，才在群内发送简短提醒。\n默认关闭、静默处理；开启后将尽量在设定时间后撤回提醒。', p0=_ui('关闭（不在群内通知）') if conf['word_notice_seconds'] == 0 else str(conf['word_notice_seconds']) + _ui(' 秒后自动撤回'))
            )
            keyboard.extend([
                [self._button(_ui('🔕 关闭'), _ui('wn:{p0}:0', p0=chat_id)),
                 self._button(_ui('10 秒'), _ui('wn:{p0}:10', p0=chat_id))],
                [self._button(_ui('30 秒'), _ui('wn:{p0}:30', p0=chat_id)),
                 self._button(_ui('60 秒'), _ui('wn:{p0}:60', p0=chat_id))],
                [self._button(_ui('⬅️ 返回违禁词'), _ui('v:{p0}:words', p0=chat_id))],
            ])
        elif what == "domains":
            domains = [r["value"] for r in self.store.filters(chat_id, "domain")]
            text = _ui('🌐 允许的域名：\n') + (join_text('\n', domains[:60]) if domains else _ui('暂无'))
            text += _ui('\n\n在群内用 /allowdomain example.com 添加。')
        elif what == "logs":
            text = (
                _ui('🧾 独立管理日志群：{p0}\n日志群必须和当前群不同，并且你和机器人都需要具备日志目标群的管理权限。\n请在日志群发送 /id 获取群 ID，再回当前群：\n/setlog -1001234567890\n关闭日志：/setlog off\n自动删除和禁言默认不在当前群公开播报。', p0=conf['log_chat_id'] or _ui('尚未设置'))
            )
        elif what == "stats":
            data = self.store.stats(chat_id)
            text = (_ui('📊 今日统计\n消息 {p0} 条\n删除 {p1} 条\n入群 {p2} 人\n处罚 {p3} 次', p0=data.get('messages', 0), p1=data.get('deleted', 0), p2=data.get('joins', 0), p3=data.get('actions', 0)))
        elif what == "replies":
            entries = self.store.replies(chat_id)
            text = _ui('💬 本群自动回复：\n') + (
                join_text('\n', (_ui('{p0} {p1}', p0='🖼️' if entry['media_type'] else '💬', p1=entry['keyword']) for entry in entries[:40])) or _ui('暂无'))
            text += _ui('\n\n添加：/addreply 词1,词2 | 回复文字 | 按钮 - https://example.com')
            text += _ui('\n可回复已有图片或视频并发送添加命令，复用媒体。')
        elif what == "points":
            conf = self.store.settings(chat_id)
            ranking = self.store.point_top(chat_id, 8)
            text = (
                _ui('🪙 签到积分：{p0}\n基础奖励：{p1} 分；连续签到最多加 6 分\n排行（用户ID / 积分）：\n', p0=_ui('开启') if conf['points_enabled'] else _ui('关闭'), p1=conf['checkin_award'])
                + (join_text('\n', (_ui('{p0}. {p1} — {p2}', p0=i, p1=r['user_id'], p2=r['balance']) for i, r in enumerate(ranking, 1))) or _ui('暂无记录'))
                + _ui('\n\n管理员：/pointmode on|off、/pointrate 5\n回复成员 /pointadd 20 或 /pointtake 20\n所有成员：/checkin、/points、/pointtop')
            )
        elif what == "photo":
            conf = self.store.settings(chat_id)
            text = (
                _ui('🖼️ 重复图片拦截：{p0}\n相同图片：{p1} 秒内连续{p2} 次触发\n只针对同一群同一成员的 Telegram 图片 file_unique_id。\n不能识别单张广告图片、二维码或重新压缩后的图片。\n\n/imageflood on|off\n/imagelimit 3\n/imagewindow 60', p0=_ui('开启') if conf['repeat_photo_enabled'] else _ui('关闭'), p1=conf['repeat_photo_window'], p2=conf['repeat_photo_limit'])
            )
        elif what == "shop":
            items = self.economy.items(chat_id)
            pending = self.economy.orders(chat_id, status="pending", limit=40)
            text = (_ui('🎁 群内虚拟奖励商城\n等待人工核销：{p0} 笔（最多显示前40）\n', p0=len(pending))
                    + (join_text('\n', (_ui('#{p0} {p1} · {p2} 分 · 余{p3}', p0=item['id'], p1=item['title'], p2=item['price'], p3=item['stock']) for item in items[:18])) or _ui('暂无上架商品'))
                    + _ui('\n\n/shopadd 名称 | 单价 | 库存 | 说明\n/shoporders /shopdone 编号 /shoprefund 编号\n成员：/shop /buy 编号 /myorders'))
        elif what == "raffle":
            raffle = self.raffles.active(chat_id)
            history = self.raffles.history(chat_id, limit=5)
            text = _ui('🎉 免费群抽奖（不收费、不消耗积分）\n')
            text += (_ui('正在开放 #{p0} {p1}\n', p0=raffle['id'], p1=raffle['title'])
                     if raffle else _ui('当前没有开放活动\n'))
            text += (join_text('\n', (_ui('#{p0} {p1} [{p2}]', p0=r['id'], p1=r['title'], p2=r['status']) for r in history)) or _ui('暂无历史'))
            text += (_ui('\n\n/rafflestart 标题 /raffledraw /rafflecancel\n成员：/raffle /joinraffle'))
        elif what == "ocr":
            settings = self.store.settings(chat_id)
            text = (
                _ui('🔎 图片文字 OCR 模式：{p0}\n引擎：{p1}\n最低可信度：{p2}\n仅扫描普通成员的 Telegram 照片，使用本机 Tesseract。\n命中至少 4 字的违禁词才记录或处理，不识别二维码。\n\n/ocr review 审核日志（推荐）\n/ocr delete 高置信自动删除（误判风险）\n/ocr off 关闭 /ocrconfidence 70 设置可信度\nOCR_ENABLED=1 且容器安装 Tesseract 才能运行。', p0=settings['ocr_mode'], p1=_ui('已安装并启用') if self.ocr.available else _ui('未启用/未安装'), p2=settings['ocr_confidence'])
            )
        elif what == "stats7":
            data = self.store.stats_days(chat_id, 7)
            text = (_ui('📈 最近7天（包含今天）\n消息 {p0} 条\n删除 {p1} 条\n入群 {p2} 人\n处罚 {p3} 次', p0=data.get('messages', 0), p1=data.get('deleted', 0), p2=data.get('joins', 0), p3=data.get('actions', 0)))
        elif what == "activity":
            rows = self.store.top_activity(chat_id, days=7)
            text = _ui('🏆 最近7天活跃排行（仅记录用户 ID）\n') + (
                join_text('\n', (_ui('{p0}. {p1}：{p2} 条', p0=i, p1=r['user_id'], p2=r['count']) for i, r in enumerate(rows, 1))) or _ui('暂无非命令消息'))
            text += _ui('\n历史明细最多保留 30 天。')
        elif what == "schedules":
            tasks = self.store.schedules(chat_id)
            text = _ui('📅 定时公告\n') + (
                join_text('\n', (_ui('#{p0} {p1} [{p2}] {p3}', p0=r['id'], p1=r['title'][:25], p2=r['mode'], p3=_ui('开启') if r['enabled'] else _ui('暂停/结束')) for r in tasks)) or _ui('暂无定时任务'))
            text += _ui('\n\n在群内使用 /schedule 查看新增、暂停和删除的详细用法。')
            for r in tasks[:15]:
                keyboard.append([
                    self._button(_ui('#{p0} {p1}', p0=r['id'], p1=r['title'][:30]), _ui('s:{p0}:{p1}', p0=chat_id, p1=r['id']))
                ])
        elif what == "help":
            text = self.help_text()
        else:
            text = _ui('该功能暂不可用。')
        text, keyboard = self.admin_ui.decorate(chat_id, what, text, keyboard)
        self.feature_help.add_admin(keyboard, chat_id, what)
        keyboard.append([self._button(_ui('⬅️ 返回设置'), _ui('g:{p0}', p0=chat_id))])
        return self._edit_or_send(
            user_id, message_id, text, keyboard)

    def _private_schedule(self, user_id, chat_id, task_id, action, message_id, query_id):
        item = self.store.schedule(chat_id, task_id)
        if not item:
            self.answer(query_id, _ui('任务不存在或已经删除。'), alert=True)
            return self._private_view(
                user_id, chat_id, "schedules", message_id, verified_admin=True)
        if action == "sx":
            self.store.delete_schedule(chat_id, task_id)
            self.log(chat_id, _ui('删除定时公告 #{p0}', p0=task_id), actor=user_id)
            self.answer(query_id, _ui('任务已删除'))
            return self._private_view(
                user_id, chat_id, "schedules", message_id, verified_admin=True)
        if action == "sp":
            enabled = not bool(item["enabled"])
            if enabled:
                try:
                    next_run = next_occurrence(
                        item["mode"], item["rule"], self.store.tz)
                except ValueError as exc:
                    self.answer(query_id, error_text(exc)[:190], alert=True)
                    return
            else:
                next_run = None
            self.store.toggle_schedule(chat_id, task_id, enabled, next_run=next_run)
            self.log(chat_id, _ui('定时公告 #{p0} {p1}', p0=task_id, p1=_ui('开启') if enabled else _ui('暂停')),
                     actor=user_id)
            self.answer(query_id, _ui('操作已保存'))
            item = self.store.schedule(chat_id, task_id)
        else:
            self.answer(query_id)
        text = (
            _ui('📅 定时公告 #{p0}\n标题：{p1}\n发送频率：{p2} {p3}\n当前状态：{p4}\n下次计划：{p5}\n内容：\n{p6}', p0=item['id'], p1=item['title'], p2=item['mode'], p3=item['rule'], p4=_ui('运行中') if item['enabled'] else _ui('暂停/结束'), p5=format_run(item['next_run'], self.store.tz), p6=item['body'][:1400]))
        keyboard = []
        if action == "sd":
            text += _ui('\n\n⚠️ 确认永久删除？')
            keyboard.append([
                self._button(_ui('✅ 确认删除'), _ui('sx:{p0}:{p1}', p0=chat_id, p1=task_id)),
                self._button(_ui('❌ 取消'), _ui('s:{p0}:{p1}', p0=chat_id, p1=task_id)),
            ])
        else:
            keyboard.append([
                self._button(_ui('▶️ 恢复') if not item["enabled"] else _ui('⏸ 暂停'),
                             _ui('sp:{p0}:{p1}', p0=chat_id, p1=task_id)),
                self._button(_ui('🗑 删除'), _ui('sd:{p0}:{p1}', p0=chat_id, p1=task_id)),
            ])
        if action != "sd":
            self.feature_help.add_admin(keyboard, chat_id, "schedules")
        keyboard.append([self._button(_ui('⬅️ 返回公告列表'), _ui('v:{p0}:schedules', p0=chat_id))])
        return self._edit_or_send(user_id, message_id, text, keyboard)

    @staticmethod
    def help_text():
        return (
            _ui('🤖 中文群管命令（在群内使用）\n/panel 私聊管理面板 /settings 查看开关\n/menu 成员按钮菜单（签到、积分、兑换、抽奖、群规）\n/links on|off /forward on|off /flood on|off\n/captcha on|off /welcome on|off\n/addword 词 /delword 词 /words\n/addwords 词1,词2 /delwords 词1,词2\n/spam on|off /mediaflood on|off /automute on|off\n/nameguard on|off /guardset name_length_limit 40\n/wordloose on|off /wordexcept 词 | 完整文本\n/wordtest 文本 /guardset flood_limit 7\n/allowdomain 域名 /deldomain 域名 /domains\n/setrules 群规文字 /rules 查看群规\n/setwelcome 欢迎语（{name} 自动替换）\n/addreply 词1,词2 | 回复 | 按钮 - https://网址\n/delreply 关键词 /replies；可回复图片/视频添加媒体\n/setlog -100... 设置日志群；/setlog off 关闭\n/stats 今日统计 /statweek 最近7天 /top7 活跃排行\n/schedule 定时公告管理 /bots on|off 防新机器人\n/checkin 签到 /points 我的积分 /pointtop 排行榜\n/transfer 20 回复同群成员，点击确认后转积分\n/shop 查看虚拟奖励 /buy 编号 /myorders\n/shopadd 名称 | 价格 | 库存 | 说明（管理员）\n/shoporders /shopdone 编号 /shoprefund 编号\n/raffle /joinraffle 免费参与\n/rafflestart 标题 /raffledraw /rafflecancel（管理员）\n/ocr review|delete|off /ocrconfidence 70 图片审核\n/pointmode on|off /pointrate 5 管理签到\n回复成员 /pointadd 20、/pointtake 20；/pointlog\n/imageflood on|off /imagelimit 3 /imagewindow 60\n/check 检查权限 /id 查看群 ID\n回复成员消息：/warn、/unwarn、/mute 60、/unmute、/ban、/kick、/dban；/unban 用户ID\n群内不公开播报成功的封禁，管理员发的管理命令不会自动删除。'))

    def _private_word_input(self, uid, chat_id, operation, text):
        """One-shot text editor, scoped to the verified group/admin identity."""
        if len(text) > 3500:
            raise ValueError(_ui('输入太长，每次最多 3500 字'))
        if operation in ("add", "del"):
            labels = [part.strip() for part in re.split(
                r"[,，、;\n]+", text) if part.strip()]
            if not 1 <= len(labels) <= 80:
                raise ValueError(_ui('每次请输入 1–80 条，以逗号或换行分隔'))
            pairs = []
            for label in labels:
                key = normalize(label)
                if not 2 <= len(key) <= 80:
                    raise ValueError(_ui('每条违禁词应为 2–80 字，未保存任何内容'))
                pairs.append((key, label))
            if operation == "add":
                count = self.store.add_filters_bulk(chat_id, "word", pairs)
            else:
                count = sum(self.store.del_filter(chat_id, "word", key)
                            for key in {k for k, _label in pairs})
            self.log(chat_id, _ui('私聊批量{p0} {p1} 条违禁词', p0=_ui('添加') if operation == 'add' else _ui('删除'), p1=count), actor=uid)
            return _ui('✅ 已{p0} {p1} 条违禁词。', p0=_ui('导入') if operation == 'add' else _ui('删除'), p1=count)
        if operation in ("except", "unexcept"):
            phrase, sep, context = text.partition("|")
            keyword, sentence = normalize(phrase.strip()), normalize(context.strip())
            if not sep or not 2 <= len(keyword) <= 80 or not 3 <= len(sentence) <= 200:
                raise ValueError(_ui('请输入：违禁词 | 完整允许文本（例如：广告 | 学校广告设计）'))
            if operation == "except" and not context_matches(keyword, sentence, self.store.settings(chat_id)):
                raise ValueError(_ui('允许文本必须包含要豁免的违禁词'))
            key = _ui('{p0}|{p1}', p0=keyword, p1=sentence)
            if operation == "except":
                self.store.add_filter(chat_id, "word_exception", key, key)
            else:
                self.store.del_filter(chat_id, "word_exception", key)
            self.log(chat_id, _ui('通过私聊修改关键词精确例外'), actor=uid)
            return _ui('✅ 已更新精确例外（只对完整相同的消息豁免对应关键词）。')
        if operation == "test":
            settings = self.store.settings(chat_id)
            hit = self.store.word_policy(chat_id).hit({"text": text},
                loose=settings["word_loose_match"], settings=settings)
            return _ui('🧪 测试结果：') + (describe_hit(hit, settings) if hit else _ui('没有命中违禁词')) + _ui('\n此操作不会处罚任何成员。')
        raise ValueError(_ui('未知的违禁词编辑操作'))

    def _private_message(self, message):
        sender = message.get("from", {})
        uid = sender.get("id")
        text = (message.get("text") or "").strip()
        if not uid:
            return
        cmd = text.split(maxsplit=1)[0].split("@")[0].lower() if text else ""
        if cmd in ("/start", "/groups", "/panel", "/help", "/commands", "/language"):
            self.admin_ui.cancel(uid)
        elif self.admin_ui.input(message):
            return
        if cmd == "/cancel" and uid in self._word_wizards:
            chat_id, _operation, _expires = self._word_wizards.pop(uid)
            return self.send(uid, _ui('✅ 已取消本次编辑。'),
                             reply_markup=self._word_navigation(chat_id))
        if cmd == "/language":
            self._word_wizards.pop(uid, None)
            return self.language_ui.page(uid)
        if cmd in ("/start", "/groups", "/panel"):
            self._word_wizards.pop(uid, None)
            payload = text.partition(" ")[2]
            if cmd == "/start" and payload == "language":
                return self.language_ui.page(uid)
            if cmd == "/start" and re.fullmatch(r"member_\d{1,18}", payload):
                return self.member_ui.render(-int(payload[7:]), uid)
            self._private_groups(uid)
        elif cmd == "/id":
            self.send(uid, _ui('🆔 你的 Telegram 数字ID：{p0}', p0=uid))
        elif cmd in ("/help", "/commands"):
            self._word_wizards.pop(uid, None)
            self.send(uid, self.help_text())
        elif cmd == "/health" and uid in self.owner_ids:
            self.send(uid, _ui('✅ 数据库连接正常，机器人运行中。'))
        elif uid in self._word_wizards and not message.get("sender_chat"):
            chat_id, operation, expiry = self._word_wizards.pop(uid)
            keyboard = self._word_navigation(chat_id)
            if time.monotonic() >= expiry:
                return self.send(uid, _ui('⏰ 编辑已过期，请重新进入群管理面板。'),
                                 reply_markup=keyboard)
            if not self.store.chat(chat_id) or not self.is_admin(chat_id, uid):
                return self.send(uid, _ui('⚠️ 当前已无该群管理权限，未修改任何规则。'),
                                 reply_markup=keyboard)
            try:
                result = self._private_word_input(uid, chat_id, operation, text)
            except ValueError as exc:
                self.send(uid, _ui('⚠️ {p0}\n如需重试，请重新点击编辑按钮。', p0=error_text(exc)[:180]),
                          reply_markup=keyboard)
                return
            self.send(uid, result, reply_markup=keyboard)
        else:
            self.send(uid, _ui('请发送 /start 打开中文群管理面板，或 /help 查看命令。'))

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
            return self.answer(qid, _ui('转账按钮格式不正确'), alert=True)
        if group.get("id") != cid or group.get("type") not in GROUP_TYPES:
            return self.answer(qid, _ui('只能在发起转账的原群操作'), alert=True)
        intent = self.economy.get_transfer(cid, transfer_id)
        if not intent or intent["source_id"] != uid or intent["nonce"] != parts[3]:
            return self.answer(qid, _ui('你不是转账发起者，或按钮已失效'), alert=True)
        if parts[0] == "et" and intent["status"] == "pending":
            # A removed/blocked member must not transfer after leaving a chat.
            try:
                left = self.api.call("getChatMember", chat_id=cid, user_id=uid)
                right = self.api.call(
                    "getChatMember", chat_id=cid, user_id=intent["target_id"])
            except TelegramAPIError:
                return self.answer(qid, _ui('暂时无法核验群成员身份，请重试'), alert=True)
            if (not is_current_member(left) or not is_current_member(right)
                    or self.store.pending(cid, uid) or self.store.pending(cid, intent["target_id"])):
                return self.answer(qid, _ui('成员身份未确认、已离群或验证未完成，转账不能执行'), alert=True)
        try:
            result = self.economy.confirm_transfer(
                cid, transfer_id, parts[3], uid, message.get("message_id"),
                accept=parts[0] == "et")
        except ValueError as exc:
            return self.answer(qid, error_text(exc)[:185], alert=True)
        label = {
            "executed": _ui('✅ 转账成功'),
            "cancelled": _ui('❌ 转账已取消'),
            "expired": _ui('⏳ 确认超时，未扣除积分'),
        }.get(result["status"], _ui('转账已处理'))
        self.answer(qid, label)
        if result["created"] or result["status"] == "expired":
            self.api.safe(
                "editMessageText", chat_id=cid,
                message_id=message["message_id"],
                text=_ui('{p0}\n发起人：{p1}\n积分变动仅限群内虚拟积分。', p0=label, p1=uid))
        if result["created"] and result["status"] == "executed":
            self.log(cid, _ui('确认积分转赠 {p0} 分', p0=result['amount']),
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
        if data.startswith("lang:"):
            return self.language_ui.callback(query)
        if data.startswith(("et:", "ec:")):
            return self._transfer_callback(query)
        if data.startswith("verify:"):
            self._verify(query)
            return
        if data.startswith("pc:"):
            return self.member_ui.callback(query)
        # Management callbacks work only in the requesting user's private chat.
        if chat.get("type") != "private" or chat.get("id") != user_id:
            self.answer(qid, _ui('请私聊机器人操作。'), alert=True)
            return
        if data == "groups":
            self._word_wizards.pop(user_id, None)
            self.answer(qid)
            return self._private_groups(user_id, message.get("message_id"))
        parts = data.split(":")
        if len(parts) not in (2, 3, 4, 5) or parts[0] not in (
            "g", "t", "v", "n", "w", "wl", "ws", "wp", "wn",
            "fs", "fp", "fn", "fh",
            "s", "sd", "sp", "sx", "ad", "ac"):
            self.answer(qid, _ui('未知或失效的按钮'))
            return
        try:
            chat_id = int(parts[1])
        except ValueError:
            self.answer(qid, _ui('按钮参数无效'))
            return
        if not self.store.chat(chat_id) or not self.is_admin(chat_id, user_id):
            self.answer(qid, _ui('你不是该群管理员。'), alert=True)
            return
        if parts[0] in ("ad", "ac"):
            self._word_wizards.pop(user_id, None)
            return self.admin_ui.callback(query, chat_id, parts)
        if parts[0] == "fh":
            if len(parts) != 3 or parts[2] not in ADMIN_PAGES:
                return self.answer(qid, _ui('未知功能说明，请重新打开功能页。'), alert=True)
            self.admin_ui.cancel(user_id)
            self._word_wizards.pop(user_id, None)
            self.answer(qid)
            return self.feature_help.admin(user_id, chat_id, parts[2], message.get("message_id"))
        self.admin_ui.cancel(user_id)
        if parts[0] != "w":
            self._word_wizards.pop(user_id, None)
        if parts[0] in ("fs", "fp", "fn"):
            if len(parts) != 3:
                return self.answer(qid, _ui('防刷屏按钮参数无效'), alert=True)
            operation, setting = parts[0], parts[2]
            conf = self.store.settings(chat_id)
            if operation == "fs":
                if setting not in ("on", "off"):
                    return self.answer(qid, _ui('防刷屏状态参数无效'), alert=True)
                on = setting == "on"
                if conf["anti_flood"] == on:
                    return self.answer(qid, _ui('当前已经是此状态'))
                if (on and not conf["anti_flood"]
                        and not self.bot_can(chat_id, "can_delete_messages")):
                    return self.answer(qid, _ui('机器人缺少删除消息的权限'), alert=True)
                self.store.set(chat_id, "anti_flood", on)
                description = _ui('防刷屏：{p0}', p0=_ui('启用') if on else _ui('关闭'))
                page = "flood"
            elif operation == "fp":
                if setting not in ("delete", "mute"):
                    return self.answer(qid, _ui('无效的防刷屏处罚方式'), alert=True)
                if conf["flood_penalty"] == setting:
                    return self.answer(qid, _ui('当前已经是此处罚方式'))
                if setting == "mute":
                    if not self.is_supergroup(chat_id):
                        return self.answer(qid, _ui('禁言仅支持超级群组'), alert=True)
                    if (not self.bot_can(chat_id, "can_delete_messages")
                            or not self.bot_can(chat_id, "can_restrict_members")):
                        return self.answer(
                            qid, _ui('需要删除消息与限制成员两项权限'), alert=True)
                self.store.set(chat_id, "flood_penalty", setting)
                description = (_ui('防刷屏处罚：仅删除') if setting == "delete"
                               else _ui('防刷屏处罚：删除并临时禁言'))
                page = "floodpenalty"
            else:
                try:
                    seconds = int(setting)
                except ValueError:
                    return self.answer(qid, _ui('无效的提醒时间'), alert=True)
                if seconds not in (0, 10, 30, 60):
                    return self.answer(qid, _ui('提醒时间只能选关闭/10/30/60秒'), alert=True)
                if conf["flood_notice_seconds"] == seconds:
                    return self.answer(qid, _ui('当前已经是此提醒设置'))
                if seconds and not self.bot_can(chat_id, "can_delete_messages"):
                    return self.answer(qid, _ui('机器人缺少删除消息权限'), alert=True)
                self.store.set(chat_id, "flood_notice_seconds", seconds)
                description = (_ui('防刷屏提醒：关闭') if seconds == 0
                               else _ui('防刷屏提醒：约 {p0} 秒', p0=seconds))
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
                return self.answer(qid, _ui('违禁词按钮参数无效'), alert=True)
            operation, setting = parts[0], parts[2]
            if operation == "wl":
                try:
                    page = int(setting)
                except ValueError:
                    return self.answer(qid, _ui('页码无效'), alert=True)
                if page < 0:
                    return self.answer(qid, _ui('页码超出范围'), alert=True)
                self.answer(qid)
                return self._private_word_list(
                    user_id, chat_id, page, message.get("message_id"),
                    verified_admin=True)
            conf = self.store.settings(chat_id)
            if operation == "ws":
                if setting not in ("on", "off"):
                    return self.answer(qid, _ui('开关参数无效'), alert=True)
                new_value = setting == "on"
                if (new_value and not conf["word_filter_enabled"]
                        and not self.bot_can(chat_id, "can_delete_messages")):
                    return self.answer(qid, _ui('机器人缺少删除消息权限'), alert=True)
                self.store.set(chat_id, "word_filter_enabled", new_value)
                label = _ui('违禁词状态：{p0}', p0=_ui('启用') if new_value else _ui('关闭'))
            elif operation == "wp":
                if setting not in ("delete", "mute"):
                    return self.answer(qid, _ui('处罚选项无效'), alert=True)
                if setting == "mute":
                    if not self.is_supergroup(chat_id):
                        return self.answer(qid, _ui('禁言只支持超级群组'), alert=True)
                    if (not self.bot_can(chat_id, "can_delete_messages")
                            or not self.bot_can(chat_id, "can_restrict_members")):
                        return self.answer(
                            qid, _ui('机器人需要删除消息和限制成员权限'), alert=True)
                self.store.set(chat_id, "word_penalty", setting)
                label = _ui('处罚：仅删除') if setting == "delete" else _ui('处罚：删除并禁言')
            else:
                try:
                    seconds = int(setting)
                except ValueError:
                    return self.answer(qid, _ui('提醒时间格式无效'), alert=True)
                if seconds not in (0, 10, 30, 60):
                    return self.answer(qid, _ui('提醒时间只能选关闭/10/30/60秒'), alert=True)
                if seconds and not self.bot_can(chat_id, "can_delete_messages"):
                    return self.answer(qid, _ui('机器人缺少删除消息权限'), alert=True)
                self.store.set(chat_id, "word_notice_seconds", seconds)
                label = _ui('删除提醒：关闭') if not seconds else _ui('删除提醒：约 {p0} 秒', p0=seconds)
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
                return self.answer(qid, _ui('未知违禁词管理操作'), alert=True)
            if len(self._word_wizards) > 2000:
                now = time.monotonic()
                for old_uid, (_cid, _act, until) in list(self._word_wizards.items()):
                    if until <= now:
                        del self._word_wizards[old_uid]
            self._word_wizards[user_id] = (chat_id, parts[2], time.monotonic() + 300)
            self.answer(qid, _ui('请在私聊中输入内容'))
            prompts = {
                "add": _ui('➕ 发送违禁词，多个词用逗号或换行分隔，每次最多80条'),
                "del": _ui('➖ 发送要删除的词，多个词用逗号或换行分隔'),
                "except": _ui('🛡️ 添加精确例外：违禁词 | 完整允许文本'),
                "unexcept": _ui('❌ 删除精确例外：违禁词 | 完整允许文本'),
                "test": _ui('🧪 发送需要测试的文字（不会自动删除或处罚）'),
            }
            self.send(user_id, _ui('{p0}\n有效期5分钟；发送 /cancel 取消。\n所有操作仅影响当前选择的群组。', p0=prompts[parts[2]]),
                      reply_markup=self._word_navigation(chat_id))
            return
        if parts[0] == "g" and len(parts) == 2:
            self.answer(qid)
            return self._private_panel(
                user_id, chat_id, message.get("message_id"),
                verified_admin=True)
        if parts[0] == "n":
            if len(parts) != 5:
                return self.answer(qid, _ui('数字设置按钮参数无效'), alert=True)
            key, raw_delta, origin = parts[2], parts[3], parts[4]
            controls = PAGE_NUMBERS.get(origin, ())
            steps = {name: step for name, _label, step in controls}
            if key not in NUMBER_LIMITS or key not in steps:
                return self.answer(qid, _ui('没有此数字设置项'), alert=True)
            try:
                delta = int(raw_delta)
            except ValueError:
                return self.answer(qid, _ui('设置变化量无效'), alert=True)
            if abs(delta) != steps[key]:
                return self.answer(qid, _ui('不允许自定义按钮变化量'), alert=True)
            previous = self.store.settings(chat_id)[key]
            low, high = NUMBER_LIMITS[key]
            proposed = previous + delta
            if not low <= proposed <= high:
                return self.answer(qid, _ui('范围必须在 {p0}–{p1} 之间', p0=low, p1=high), alert=True)
            self.store.set(chat_id, key, proposed)
            self.answer(qid, _ui('✅ {p0} = {p1}', p0=key, p1=proposed))
            updated = self._private_view(
                user_id, chat_id, origin, message.get("message_id"),
                verified_admin=True)
            self.log(chat_id, _ui('设置 {p0}：{p1} → {p2}', p0=key, p1=previous, p2=proposed), actor=user_id)
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
                self.answer(qid, _ui('任务编号无效'), alert=True)
                return
            return self._private_schedule(user_id, chat_id, task_id,
                                          parts[0], message.get("message_id"), qid)
        action = parts[2] if len(parts) >= 3 else ""
        if action not in TOGGLES:
            self.answer(qid, _ui('不允许修改这个设置。'), alert=True)
            return
        if parts[0] != "t" or len(parts) not in (3, 4):
            return self.answer(qid, _ui('按钮参数无效'), alert=True)
        origin = parts[3] if len(parts) == 4 else None
        if origin is not None and action not in {
            key for _label, key in PAGE_SWITCHES.get(origin, ())
        }:
            return self.answer(qid, _ui('非法功能页开关'), alert=True)
        needed = ("can_restrict_members" if action in (
            "verify_enabled", "block_new_bots", "auto_mute_enabled",
            "name_guard_enabled", "raid_enabled")
                  else "can_delete_messages")
        if action not in ("verify_enabled", "block_new_bots", "auto_mute_enabled",
                          "name_guard_enabled", "raid_enabled",
                          "block_links", "block_forward", "anti_flood",
                          "repeat_photo_enabled", "spam_guard_enabled",
                          "word_loose_match", "media_flood_enabled",
                          "block_external_channels"):
            needed = None
        current = self.store.settings(chat_id)[action]
        if needed and not current and not self.bot_can(chat_id, needed):
            self.answer(qid, _ui('机器人缺少必要管理员权限，请先授予权限。'), alert=True)
            return
        if (action in ("verify_enabled", "block_new_bots", "auto_mute_enabled",
                       "name_guard_enabled", "raid_enabled") and not current
                and not self.is_supergroup(chat_id)):
            self.answer(qid, _ui('该功能仅支持超级群组。'), alert=True)
            return
        self.store.set(chat_id, action, not current)
        self.answer(qid, _ui('设置已保存'))
        if origin:
            self._private_view(
                user_id, chat_id, origin, message.get("message_id"),
                verified_admin=True)
        else:
            self._private_panel(
                user_id, chat_id, message.get("message_id"),
                verified_admin=True)
        self.log(chat_id, _ui('开关 {p0} → {p1}', p0=action, p1=not current), actor=user_id)

    def _verify(self, query):
        qid = query["id"]
        user_id = query["from"]["id"]
        parts = query.get("data", "").split(":")
        if len(parts) != 5:
            return self.answer(qid, _ui('验证按钮无效。'), alert=True)
        try:
            chat_id, intended, nonce, chosen = (int(parts[1]), int(parts[2]),
                                                parts[3], int(parts[4]))
        except (ValueError, IndexError):
            return self.answer(qid, _ui('验证参数错误。'), alert=True)
        if user_id != intended:
            return self.answer(qid, _ui('只能由需要验证的新成员点击。'), alert=True)
        # Bind callback to the original group and message to block replay.
        message = query.get("message") or {}
        if (message.get("chat") or {}).get("id") != chat_id:
            return self.answer(qid, _ui('验证来源不匹配。'), alert=True)
        pending = self.store.pending(chat_id, user_id)
        if not pending or pending["nonce"] != nonce or pending["message_id"] != message.get("message_id"):
            return self.answer(qid, _ui('验证已失效。'), alert=True)
        if pending["expires_at"] <= time.time():
            return self.answer(qid, _ui('验证已过期，请联系管理员。'), alert=True)
        attempt_limit = self.store.settings(chat_id)["verify_attempt_limit"]
        if pending["attempts"] >= attempt_limit:
            return self.answer(qid, _ui('答错次数已用完，请联系管理员或等待超时后重新加入。'), alert=True)
        if chosen != pending["answer"]:
            pending = self.store.verification_mistake(chat_id, user_id)
            left = max(0, attempt_limit - pending["attempts"])
            return self.answer(qid, _ui('答案不正确，剩余 {p0} 次。', p0=left), alert=True)
        if not self.unrestrict(chat_id, user_id):
            return self.answer(qid, _ui('机器人缺少解除限制的权限，请联系管理员。'), alert=True)
        self.store.del_pending(chat_id, user_id)
        self.api.safe("deleteMessage", chat_id=chat_id, message_id=message["message_id"])
        self.answer(qid, _ui('✅ 验证成功，欢迎加入！'))
        self.log(chat_id, _ui('新人验证成功'), target=user_id)

    def _new_members(self, message):
        chat = message["chat"]
        chat_id = chat["id"]
        settings = self.store.settings(chat_id)
        for member in message.get("new_chat_members", []):
            uid = member.get("id")
            if uid is None or uid == self.bot_id or member.get("is_bot"):
                if (uid and uid != self.bot_id and member.get("is_bot")
                    and settings["block_new_bots"]
                    and self.can_target(chat_id, uid)
                    and self.bot_can(chat_id, "can_restrict_members")):
                    removed = self.api.safe("banChatMember", chat_id=chat_id,
                                            user_id=uid)
                    if removed is True:
                        self.api.safe("unbanChatMember", chat_id=chat_id,
                                      user_id=uid, only_if_banned=True)
                        self.log(chat_id, _ui('拦截新加入的第三方机器人'), target=uid)
                continue
            self.store.stat(chat_id, "joins")
            if self.store.pending(chat_id, uid):
                continue
            active = bool(self.store.raid_until(chat_id))
            if (active or settings["raid_enabled"] or settings["verify_enabled"]
                    or settings["name_guard_enabled"]):
                try:
                    status = self.api.call("getChatMember", chat_id=chat_id, user_id=uid)
                except TelegramAPIError:
                    continue
                if (not isinstance(status, dict) or admin_status(status)
                        or status.get("status") != "member"):
                    # Never replace somebody else's restriction with a challenge
                    # that would grant permissions back after solving it.
                    continue
            if (settings["raid_enabled"] and not active
                    and self.join_guard.arrival(chat_id, uid,
                        limit=settings["raid_join_limit"], window=settings["raid_window"])
                    and self.is_supergroup(chat_id)
                    and self.bot_can(chat_id, "can_restrict_members")):
                self.store.set_raid_until(chat_id, int(time.time()) + settings["raid_duration"] * 60)
                self.join_guard.clear(chat_id)
                self.store.stat(chat_id, "raid_triggers")
                self.log(chat_id, _ui('集中入群保护触发，新加入成员需完成验证'))
                active = True
            needs_verify = settings["verify_enabled"] or active
            if (settings["name_guard_enabled"] and not needs_verify
                    and self.is_supergroup(chat_id)):
                nickname = (str(member.get("first_name") or "") + " "
                            + str(member.get("last_name") or "")).strip()
                if len(nickname) > settings["name_length_limit"]:
                    if self.bot_can(chat_id, "can_restrict_members"):
                        restricted = self.api.safe(
                            "restrictChatMember", chat_id=chat_id, user_id=uid,
                            permissions=RESTRICTED,
                            until_date=int(time.time()) + 1800)
                        if restricted is True:
                            self.store.stat(chat_id, "actions")
                            self.log(chat_id, _ui('新成员昵称过长，限制发言30分钟'),
                                     target=uid)
                            continue
            if needs_verify:
                self._start_verification(chat_id, member, settings, verified_member=True)
            elif (settings["welcome_enabled"] and self.join_guard.welcome(
                    chat_id, limit=settings["welcome_limit"])):
                name = (member.get("first_name") or _ui('新朋友'))[:64]
                self.send(chat_id, self.store.content_text(chat_id, "welcome_text").replace("{name}", name))

    def draw_raffle(self, chat_id, actor, *, expected_id=None):
        active = self.raffles.active(chat_id)
        if not active or expected_id is not None and active["id"] != expected_id:
            raise ValueError(_ui('活动已变化或未开放，请刷新。'))
        candidates = [r[0] for r in self.store.db.execute(
            "SELECT user_id FROM raffle_entries WHERE raffle_id=? ORDER BY user_id", (active["id"],))]
        if not candidates:
            raise ValueError(_ui('没有参与者，不能开奖。'))
        # Bound network work; no scan of twenty thousand Telegram memberships.
        for _ in range(min(3, len(candidates))):
            target = candidates.pop(secrets.randbelow(len(candidates)))
            try:
                person = self.api.call("getChatMember", chat_id=chat_id, user_id=target)
            except TelegramAPIError:
                raise ValueError(_ui('暂时无法核实候选成员，未开奖，请稍后重试。')) from None
            if not isinstance(person, dict) or person.get("status") not in ("member", "restricted", "creator", "administrator", "left", "kicked"):
                raise ValueError(_ui('暂时无法核实候选成员，未开奖，请稍后重试。'))
            if person.get("status") == "restricted" and type(person.get("is_member")) is not bool:
                raise ValueError(_ui('暂时无法核实候选成员，未开奖，请稍后重试。'))
            if not is_current_member(person) or self.store.pending(chat_id, target):
                self.raffles.drop_member(chat_id, target, raffle_id=active["id"])
                continue
            return self.raffles.draw(chat_id, actor, selected_user=target, expected_id=active["id"])
        raise ValueError(_ui('本次候选已离群或验证未完成，已移除报名；请再次开奖核验下一批，活动仍开放。'))

    def _start_verification(self, chat_id, member, settings, *, verified_member=False):
        uid = member["id"]
        # Replayed join updates must not replace the challenge or reset attempts.
        if self.store.pending(chat_id, uid):
            return
        if not verified_member:
            try:
                status = self.api.call("getChatMember", chat_id=chat_id, user_id=uid)
            except TelegramAPIError:
                return
            if uid == self.bot_id or not isinstance(status, dict) or status.get("status") != "member":
                return
        if not self.is_supergroup(chat_id):
            return
        now = int(time.time())
        timeout = settings["verify_timeout"]
        if not self.bot_can(chat_id, "can_restrict_members"):
            self.log(chat_id, _ui('新人验证未启用：缺少限制成员权限'), target=uid)
            return
        # Keep the member muted until verification succeeds or timeout cleanup.
        # A timed Telegram restriction would expire before offline cleanup can kick.
        restricted = self.api.safe("restrictChatMember", chat_id=chat_id, user_id=uid,
                                   permissions=RESTRICTED, until_date=0)
        if restricted is not True:
            self.log(chat_id, _ui('无法限制新成员，跳过验证'), target=uid)
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
            str(choice), _ui('verify:{p0}:{p1}:{p2}:{p3}', p0=chat_id, p1=uid, p2=nonce, p3=index))
            for index, choice in enumerate(options[:2])],
            [self._button(
                str(choice), _ui('verify:{p0}:{p1}:{p2}:{p3}', p0=chat_id, p1=uid, p2=nonce, p3=index))
             for index, choice in enumerate(options[2:], start=2)]]
        name = (member.get("first_name") or _ui('新成员'))[:64]
        posted = self.send(chat_id, _ui('🧩 {p0}，欢迎加入！请在 {p1} 分钟内回答：{p2} + {p3} = ？（仅本人可点击）', p0=name, p1=timeout // 60, p2=a, p3=b),
                           reply_markup={"inline_keyboard": buttons})
        if not isinstance(posted, dict) or not posted.get("message_id"):
            # Don't trap a legitimate member if the prompt cannot be sent.
            self.unrestrict(chat_id, uid)
            self.store.del_pending(chat_id, uid)
            self.log(chat_id, _ui('验证题发送失败，尝试解除限制'), target=uid)
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
                    self.log(chat_id, _ui('新人验证超时，已移出群（允许重新加入）'), target=uid)
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
        if seconds not in (10, 30, 60) or reason not in (_ui('违禁词'), _ui('防刷屏')):
            return
        tick = time.monotonic()
        if tick - self._last_moderation_notice.get(chat_id, -1e10) < 30:
            return
        notice = self.send(
            chat_id, _ui('⛔ {p0}违规消息已清理（本提醒约 {p1} 秒后撤回）', p0=reason, p1=seconds),
            disable_notification=True)
        if isinstance(notice, dict) and notice.get("message_id"):
            self._last_moderation_notice[chat_id] = tick
            self.store.schedule_notice_deletion(
                chat_id, notice["message_id"], int(time.time()) + seconds)
        if len(self._last_moderation_notice) > 5000:
            self._last_moderation_notice = {
                cid: last for cid, last in self._last_moderation_notice.items()
                if tick - last < 3600}

    def _prepare_cross_flood(self, event, settings, current):
        if event is None:
            return None
        cid = current["chat"]["id"]
        rows = [r for r in event["rows"] if not r["processed"]] if event["active"] else event["rows"]
        people, valid = {}, []
        for row in rows:
            uid = row["uid"]
            if uid not in people:
                try:
                    person = self.api.call("getChatMember", chat_id=cid, user_id=uid)
                except TelegramAPIError:
                    return None
                if not isinstance(person, dict) or person.get("status") not in ADMIN_STATUSES | {"member", "restricted", "left", "kicked"}:
                    return None
                people[uid] = person
            if admin_status(people[uid]) or uid == self.bot_id or (people[uid].get("user") or {}).get("is_bot"):
                self.cross_guard.invalidate(cid, row["mid"])
            else:
                valid.append(row)
        if not any(r["mid"] == current["message_id"] for r in valid):
            return None
        if not event["active"] and (len(valid) < settings["cross_flood_limit"]
                or len({r["uid"] for r in valid}) < settings["cross_flood_users"]):
            return None
        event["rows"], event["people"] = valid, people
        self.cross_guard.activate(event, settings["cross_flood_window"])
        return event

    def _cleanup_cross_flood(self, event, current):
        cid, mid = current["chat"]["id"], current["message_id"]
        people, rows = {}, []
        for row in event["rows"]:
            if row["processed"] or row["mid"] == mid:
                continue
            uid = row["uid"]
            if uid not in people:
                try:
                    people[uid] = self.api.call("getChatMember", chat_id=cid, user_id=uid)
                except TelegramAPIError:
                    people[uid] = None
            person = people[uid]
            if not isinstance(person, dict) or person.get("status") not in {"member", "restricted", "left", "kicked"}:
                continue
            rows.append(row)
        if not rows or not self.bot_can(cid, "can_delete_messages"):
            return
        success = self.api.safe("deleteMessages", chat_id=cid, message_ids=[r["mid"] for r in rows]) is True
        for row in rows:
            # Telegram skips absent IDs in bulk deletion; distinguish request
            # success from a confirmed individual deletion and don't punish.
            self.store.audit(cid, row["mid"], row["uid"], CROSS_REASON,
                             "cleared" if success else "failed")
            if success:
                self.cross_guard.mark_processed(cid, row["mid"])
        if success:
            self.store.stat(cid, "cross_cleanup_requested", len(rows))
        self.log(cid, _ui('跨账号重复：批量清理{p0}，{p1}条；早先作者不追加处罚', p0=_ui('已提交') if success else _ui('失败'), p1=len(rows)))

    def _moderate(self, message, *, is_edit=False):
        chat = message["chat"]
        chat_id = chat["id"]
        if is_edit:
            self.cross_guard.invalidate(chat_id, message.get("message_id"))
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
            reason = _ui('外部频道发言')
            if settings["observe_only"]:
                self.store.audit(chat_id, message["message_id"], target, reason, "observed", "channel")
                return True
            deleted = (self.bot_can(chat_id, "can_delete_messages") and self.api.safe(
                "deleteMessage", chat_id=chat_id, message_id=message["message_id"]) is True)
            self.store.audit(chat_id, message["message_id"], target, reason,
                             "deleted" if deleted else "failed", "channel")
            if deleted:
                self.store.stat(chat_id, "deleted")
                self.log(chat_id, _ui('自动删除外部频道发言'), target=target)
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
        cross = self._prepare_cross_flood(self.cross_guard.check(message, settings, is_edit=is_edit), settings, message)
        policy = self.store.word_policy(chat_id)
        reason = evaluate(message, settings,
                          policy.custom,
                          [r["key"] for r in self.store.filters(chat_id, "domain")],
                          self.flood,
                          exceptions=policy.exceptions,
                          is_edit=is_edit)
        if cross:
            if settings["observe_only"] or settings["cross_flood_mode"] == "observe":
                self.store.audit(chat_id, message["message_id"], user_id, CROSS_REASON, "observed")
                for row in cross["rows"]:
                    self.cross_guard.mark_processed(chat_id, row["mid"])
            else:
                reason = CROSS_REASON
        if not reason and not is_edit and settings["repeat_photo_enabled"]:
            reason = self.photo_guard.check(
                message, limit=settings["repeat_photo_limit"],
                window=settings["repeat_photo_window"])
        word_hit = None
        if reason == _ui('违禁词'):
            word_hit = {"word": message_word_hit(message, policy.custom, loose=settings["word_loose_match"],
                                                 exceptions=policy.exceptions), "pack": "", "observe": False}
        if not reason and settings["word_filter_enabled"]:
            word_hit = policy.hit(message, loose=settings["word_loose_match"], settings=settings)
            if word_hit:
                reason = _ui('违禁词')
        if not reason and cross:
            reason = CROSS_REASON
        if reason:
            # Refresh membership only before deletion. An administrator promoted
            # during the short admin-list cache window must never be auto-deleted.
            try:
                fresh = cross["people"][user_id] if cross else self.api.call("getChatMember", chat_id=chat_id, user_id=user_id)
            except TelegramAPIError:
                # Missing membership evidence is not permission to delete.
                return False
            if not isinstance(fresh, dict) or fresh.get("status") not in ADMIN_STATUSES | {"member", "restricted", "left", "kicked"}:
                return False
            if admin_status(fresh):
                return True
            details = {"rule_key": word_hit["word"], "pack_id": word_hit["pack"],
                       "match_kind": word_hit.get("match_kind", "")} if word_hit else {}
            if (settings["observe_only"] or word_hit and word_hit["observe"]
                    or reason == CROSS_REASON and settings["cross_flood_mode"] == "observe"):
                self.store.audit(chat_id, message["message_id"], user_id, reason, "observed", **details)
                return True
            if not self.bot_can(chat_id, "can_delete_messages"):
                self.store.audit(chat_id, message["message_id"], user_id, reason, "failed", **details)
                return False
            deleted = self.api.safe("deleteMessage", chat_id=chat_id,
                                    message_id=message["message_id"])
            self.store.audit(chat_id, message["message_id"], user_id, reason,
                             "deleted" if deleted is True else "failed", **details)
            if deleted is True:
                self.cross_guard.mark_processed(chat_id, message["message_id"])
                if reason == CROSS_REASON:
                    self._cleanup_cross_flood(cross, message)
                self.store.stat(chat_id, "deleted")
                self.log(chat_id, _ui('自动删除（{p0}）', p0=reason), target=user_id)
                flood_reason = reason in FLOOD_REASONS
                keyword_reason = reason == _ui('违禁词')
                specific_minutes = (
                    settings["word_mute_minutes"]
                    if keyword_reason and settings["word_penalty"] == "mute"
                    else None)
                specific_muted = False
                specific_handled = flood_reason and settings["flood_penalty"] != "delete"
                if flood_reason:
                    result = apply_penalty(self, chat_id, user_id, message["message_id"], settings, is_edit=is_edit)
                    if result is not None:
                        self.store.audit_penalty(chat_id, message["message_id"], user_id, settings["flood_penalty"], result)
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
                                 _ui('{p0}违规，禁言 {p1} 分钟', p0=_ui('违禁词') if keyword_reason else _ui('防刷屏'), p1=specific_minutes),
                                 target=user_id)
                if keyword_reason:
                    self._post_moderation_notice(
                        chat_id, _ui('违禁词'), settings["word_notice_seconds"])
                elif flood_reason:
                    self._post_moderation_notice(
                        chat_id, _ui('防刷屏'), settings["flood_notice_seconds"])
                if (settings["auto_mute_enabled"] and not is_edit
                        and not specific_muted and not specific_handled):
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
                                     _ui('10分钟内违规 {p0} 次，自动禁言 {p1} 分钟', p0=history['recent'], p1=settings['auto_mute_minutes']),
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
            policy = self.store.word_policy(cid)
            pack_id, pack_observe, match_kind = "", False, ""
            matched = matched_words(
                text, confidence, (),
                threshold=settings["ocr_confidence"],
                exceptions=policy.exceptions, compiled=policy.ocr_rules)
            if not matched:
                for state in ("enforce", "observe"):
                    for pack, current in policy.packs:
                        if current != state:
                            continue
                        matched = matched_words(text, confidence, (), compiled=pack.ocr_rules,
                            threshold=settings["ocr_confidence"], exceptions=policy.exceptions, excluded=policy.excluded)
                        if matched:
                            pack_id, pack_observe = pack.id, state == "observe"
                            break
                    if matched:
                        break
            if (not matched or pack_observe) and math.isfinite(confidence) and confidence >= settings["ocr_confidence"]:
                variant = policy.variant_hit({"text": text}, settings, ocr=True)
                if variant and (not matched or not variant["observe"]):
                    matched, pack_id, pack_observe = [variant["word"]], variant["pack"], variant["observe"]
                    match_kind = variant["match_kind"]
            if not matched:
                continue
            try:
                person = self.api.call("getChatMember", chat_id=cid, user_id=uid)
            except TelegramAPIError:
                continue
            if admin_status(person):
                continue
            self.store.stat(cid, "ocr_hits")
            details = {"rule_key": matched[0], "pack_id": pack_id, "match_kind": match_kind}
            if mode == "review" or settings["observe_only"] or pack_observe:
                self.store.audit(cid, event["message_id"], uid, _ui('OCR违禁词'), "observed", "ocr", **details)
                # No raw image, OCR text, or untrusted URL is stored in logs.
                self.log(cid, _ui('🔎 OCR待审核 消息 {p0}：命中 {p1} / 可信度 {p2:.0f}', p0=event['message_id'], p1=join_text(',', matched), p2=confidence),
                         target=uid)
                continue
            if mode != "delete" or not self.bot_can(cid, "can_delete_messages"):
                self.store.audit(cid, event["message_id"], uid, _ui('OCR违禁词'), "failed", "ocr", **details)
                continue
            deleted = self.api.safe(
                "deleteMessage", chat_id=cid, message_id=event["message_id"])
            self.store.audit(cid, event["message_id"], uid, _ui('OCR违禁词'),
                             "deleted" if deleted is True else "failed", "ocr", **details)
            if deleted is True:
                self.cross_guard.mark_processed(cid, event["message_id"])
                self.store.stat(cid, "deleted")
                self.store.stat(cid, "ocr_deleted")
                self.log(cid, _ui('🔎 OCR自动删除消息 {p0}：命中 {p1} / 可信度 {p2:.0f}', p0=event['message_id'], p1=join_text(',', matched), p2=confidence),
                         target=uid)

    def handle_update(self, update):
        event = update.get("callback_query") or update.get("message") or update.get("edited_message") or {}
        sender = event.get("from") or {}
        chat = (event.get("message") or event).get("chat") or {}
        uid = sender.get("id")
        if chat.get("type") == "private" and uid and not self.store.has_ui_language(uid) and sender.get("language_code"):
            self.store.set_ui_language(uid, normalize_locale(sender["language_code"]))
        owner = chat.get("id") if chat.get("type") in GROUP_TYPES else uid
        token = CURRENT_LANGUAGE.set(self.store.ui_language(owner) if owner else "zh-CN")
        try:
            return self._handle_update(update)
        finally:
            CURRENT_LANGUAGE.reset(token)

    def _handle_update(self, update):
        if "channel_post" in update:
            posted = update["channel_post"]
            chat = posted.get("chat") or {}
            if chat.get("type") == "channel" and (
                posted.get("text") or "").split("@")[0] == "/id":
                self.send(chat["id"], _ui('🆔 频道 ID：{p0}', p0=chat['id']))
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
